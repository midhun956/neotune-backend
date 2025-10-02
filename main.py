from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, StreamingResponse
import yt_dlp
from ytmusicapi import YTMusic
import json
from typing import List, Dict, Any, Optional
import logging
import requests
from urllib.parse import parse_qs, urlparse
import os
import time
import random
import threading
from cachetools import TTLCache

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Neotune API", description="API for streaming music from YouTube Music")

# Configure CORS to allow requests from the Android app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize YouTube Music API client
ytmusic = YTMusic()

# LRU cache for audio URLs (max 2048 entries, 2 hour TTL)
audio_url_cache = TTLCache(maxsize=2048, ttl=7200)
# Locks for each video_id to avoid duplicate yt-dlp calls
audio_url_locks = {}
# Cache for failures (short TTL)
audio_url_failure_cache = TTLCache(maxsize=512, ttl=300)  # 5 min TTL for failures

# Function to extract expire parameter from YouTube URL
def parse_expire_from_url(url):
    try:
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)
        if 'expire' in query_params:
            return int(query_params['expire'][0])
        return int(time.time()) + 3600  # Default: 1 hour from now
    except Exception as e:
        logger.error(f"Error parsing expire from URL: {str(e)}")
        return int(time.time()) + 3600  # Default: 1 hour from now

# Cleanup function for locks to prevent memory leaks
def cleanup_locks():
    to_remove = []
    for video_id, lock in audio_url_locks.items():
        if not lock.locked():
            to_remove.append(video_id)
    
    for video_id in to_remove:
        del audio_url_locks[video_id]
    
    logger.info(f"Cleaned up {len(to_remove)} unused locks. Active locks: {len(audio_url_locks)}")

# Helper function to get or fetch and cache audio URL for a video_id
def get_or_cache_audio_url(video_id):
    # Check for recent failure
    if video_id in audio_url_failure_cache:
        return None, None, None
    
    # Periodically clean up locks (every ~100 calls)
    if random.random() < 0.01:
        cleanup_locks()
    
    lock = audio_url_locks.setdefault(video_id, threading.Lock())
    # Add timeout to lock acquisition to prevent deadlocks
    acquired = lock.acquire(timeout=10)
    if not acquired:
        logger.error(f"Timeout acquiring lock for {video_id}, possible deadlock")
        return None, None, None
    
    try:
        if video_id in audio_url_cache:
            audio_url, expire_timestamp, content_type = audio_url_cache[video_id]
            if time.time() < expire_timestamp:
                return audio_url, expire_timestamp, content_type
            else:
                del audio_url_cache[video_id]
        try:
            ydl_opts = {
                'format': 'bestaudio/best',
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'skip_download': True,
                'socket_timeout': 15,  # Add timeout for network operations
            }
            url = f"https://www.youtube.com/watch?v={video_id}"
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    audio_url_failure_cache[video_id] = True
                    return None, None, None
                if 'url' in info:
                    audio_url = info['url']
                    try:
                        head_response = requests.head(audio_url, timeout=5)
                        content_type = head_response.headers.get('Content-Type', 'audio/mpeg')
                    except Exception:
                        content_type = 'audio/mpeg'
                    expire_timestamp = parse_expire_from_url(audio_url)
                    audio_url_cache[video_id] = (audio_url, expire_timestamp, content_type)
                    return audio_url, expire_timestamp, content_type
                formats = info.get('formats', [])
                audio_formats = [f for f in formats if f.get('acodec') != 'none']
                if not audio_formats:
                    audio_formats = formats
                audio_formats.sort(key=lambda f: (
                    0 if f.get('vcodec') in (None, 'none') else 1,
                    -(f.get('abr', 0) or 0)
                ))
                if not audio_formats:
                    audio_url_failure_cache[video_id] = True
                    return None, None, None
                best_audio = audio_formats[0]
                audio_url = best_audio.get('url')
                content_type = best_audio.get('mime_type', 'audio/mpeg').split(';')[0]
                expire_timestamp = parse_expire_from_url(audio_url)
                audio_url_cache[video_id] = (audio_url, expire_timestamp, content_type)
                return audio_url, expire_timestamp, content_type
        except Exception as e:
            logger.error(f"Error extracting audio URL for {video_id}: {str(e)}")
            audio_url_failure_cache[video_id] = True
            return None, None, None
    finally:
        # Always release the lock, even if an exception occurs
        lock.release()

# Priority-based task management system
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import PriorityQueue
from enum import IntEnum

# Priority levels (lower number = higher priority)
class TaskPriority(IntEnum):
    CRITICAL = 1      # Music playback and seeking
    HIGH = 2          # Search requests
    MEDIUM = 3        # Trending songs, recommendations
    LOW = 4           # Background prefetching
    BACKGROUND = 5    # Non-essential tasks

# Task wrapper for priority queue
class PriorityTask:
    def __init__(self, priority: TaskPriority, task_id: str, func, *args, **kwargs):
        self.priority = priority
        self.task_id = task_id
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.created_at = time.time()
    
    def __lt__(self, other):
        # Lower priority number = higher priority
        if self.priority != other.priority:
            return self.priority < other.priority
        # If same priority, older tasks get priority
        return self.created_at < other.created_at

# Priority-based thread pool manager
class PriorityThreadPool:
    def __init__(self, max_workers=10):
        self.max_workers = max_workers
        self.task_queue = PriorityQueue()
        self.thread_pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="priority")
        self.running_tasks = {}
        self.task_lock = threading.Lock()
        self.stats = {
            'critical': 0,
            'high': 0,
            'medium': 0,
            'low': 0,
            'background': 0
        }
    
    def submit(self, priority: TaskPriority, task_id: str, func, *args, **kwargs):
        """Submit a task with priority"""
        task = PriorityTask(priority, task_id, func, *args, **kwargs)
        
        with self.task_lock:
            self.stats[priority.name.lower()] += 1
        
        # Submit to thread pool
        future = self.thread_pool.submit(self._execute_task, task)
        
        with self.task_lock:
            self.running_tasks[task_id] = future
        
        return future
    
    def _execute_task(self, task: PriorityTask):
        """Execute a priority task"""
        try:
            logger.info(f"Executing {task.priority.name} priority task: {task.task_id}")
            result = task.func(*task.args, **task.kwargs)
            return result
        except Exception as e:
            logger.error(f"Error executing task {task.task_id}: {str(e)}")
            raise
        finally:
            with self.task_lock:
                if task.task_id in self.running_tasks:
                    del self.running_tasks[task.task_id]
    
    def get_stats(self):
        """Get current task statistics"""
        with self.task_lock:
            return self.stats.copy()
    
    def shutdown(self):
        """Shutdown the thread pool"""
        self.thread_pool.shutdown(wait=True)

# Create priority thread pool
priority_pool = PriorityThreadPool(max_workers=15)

# Legacy thread pools for backward compatibility
prefetch_thread_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="prefetch")
download_thread_pool = ThreadPoolExecutor(max_workers=5, thread_name_prefix="download")

# Background pre-fetch for audio URLs with priority
def background_prefetch_audio_urls(video_ids, priority=TaskPriority.LOW):
    def fetch_single(vid):
        try:
            logger.info(f"Background prefetching audio URL for {vid} (priority: {priority.name})")
            get_or_cache_audio_url(vid)
            return True
        except Exception as e:
            logger.error(f"Error in background prefetch for {vid}: {str(e)}")
            return False
    
    # Submit each video_id to the priority thread pool
    for vid in video_ids:
        task_id = f"prefetch_{vid}"
        priority_pool.submit(priority, task_id, fetch_single, vid)

# High-priority prefetch for immediate playback
def critical_prefetch_audio_urls(video_ids):
    """Prefetch audio URLs with critical priority for immediate playback"""
    background_prefetch_audio_urls(video_ids, TaskPriority.CRITICAL)

@app.get("/", response_class=HTMLResponse)
def read_root():
    """
    Serves the HTML player
    """
    html_file = os.path.join(os.path.dirname(__file__), "player.html")
    if os.path.exists(html_file):
        with open(html_file, "r") as f:
            return f.read()
    else:
        return "<html><body><h1>Welcome to Neotune API</h1><p>Player HTML not found.</p></body></html>"

@app.get("/search")
def search_songs(query: str = Query(..., description="Search query"), limit: int = Query(10, description="Number of results to return")):
    start_time = time.time()
    try:
        # Try with filter='songs' first
        search_results = ytmusic.search(query, filter="songs", limit=limit)
        if not search_results:
            # Fallback: try without filter for broader results
            logger.info(f"No results for '{query}' with filter='songs', trying filter=None")
            search_results = ytmusic.search(query, filter=None, limit=limit)
        if not search_results:
            # Fallback: try a popular query
            logger.info(f"No results for '{query}' with filter=None, trying fallback query 'top hits'")
            search_results = ytmusic.search("top hits", filter="songs", limit=limit)
        
        # Prefetch the top few results in the background with HIGH priority
        video_ids = [song.get('videoId') for song in search_results[:3] if song.get('videoId')]
        if video_ids:
            background_prefetch_audio_urls(video_ids, TaskPriority.HIGH)
            
        elapsed = time.time() - start_time
        if elapsed > 2.0:
            logger.warning(f"/search for '{query}' took {elapsed:.2f}s")
        return search_results
    except Exception as e:
        logger.error(f"/search error for '{query}': {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

# Removed redundant /stream endpoint that was just redirecting to /yt_audio

@app.get("/recommended")
def get_recommended(
    video_id: str = Query(None, description="YouTube video ID"),
    genres: str = Query(None, description="Comma-separated genres"),
    languages: str = Query(None, description="Comma-separated languages"),
    artists: str = Query(None, description="Comma-separated artists"),
    limit: int = Query(10, description="Number of recommendations to return")
):
    try:
        logger.info(f"Getting recommendations for video_id={video_id}, genres={genres}, languages={languages}, artists={artists}")
        if video_id:
            # Existing logic for video-based recommendations
            try:
                recommendations = ytmusic.get_watch_playlist(video_id, limit=limit)
                tracks = recommendations.get('tracks', [])
                if tracks:
                    # Prefetch top results in the background with MEDIUM priority
                    video_ids = [song.get('videoId') for song in tracks[:3] if song.get('videoId')]
                    if video_ids:
                        background_prefetch_audio_urls(video_ids, TaskPriority.MEDIUM)
                return tracks
            except Exception as watch_error:
                logger.error(f"Error getting watch playlist: {str(watch_error)}")
        # If no video_id or failed, use user preferences
        query_parts = []
        if genres:
            query_parts.append(genres.replace(",", " "))
        if languages:
            query_parts.append(languages.replace(",", " "))
        if artists:
            query_parts.append(artists.replace(",", " "))
        query = " ".join(query_parts).strip() or "popular songs"
        logger.info(f"Recommendation query: {query}")
        search_results = ytmusic.search(query, filter="songs", limit=limit)
        if not search_results:
            logger.info(f"No results for '{query}', falling back to 'top hits'")
            search_results = ytmusic.search("top hits", filter="songs", limit=limit)
        
        # Prefetch top results in the background with MEDIUM priority
        if search_results:
            video_ids = [song.get('videoId') for song in search_results[:3] if song.get('videoId')]
            if video_ids:
                background_prefetch_audio_urls(video_ids, TaskPriority.MEDIUM)
                
        return search_results
    except Exception as e:
        logger.error(f"Error fetching recommendations: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get recommendations: {str(e)}")

@app.get("/trending")
def get_trending(limit: int = Query(20, description="Number of trending songs to return")):
    try:
        logger.info("Getting trending songs from featured playlists...")
        try:
            featured_playlists = []
            home_content = ytmusic.get_home()
            for section in home_content:
                if 'contents' in section:
                    for item in section['contents']:
                        if 'playlistId' in item:
                            featured_playlists.append({
                                'playlistId': item['playlistId'],
                                'title': item.get('title', 'Unknown Playlist')
                            })
                            if len(featured_playlists) >= 3:
                                break
                if len(featured_playlists) >= 3:
                    break
            all_songs = []
            for playlist_info in featured_playlists:
                try:
                    if 'playlistId' in playlist_info:
                        playlist_id = playlist_info['playlistId']
                        search_results = ytmusic.search(playlist_info['title'], filter="songs", limit=5)
                        if search_results and len(search_results) > 0:
                            all_songs.extend(search_results)
                except Exception as e:
                    logger.error(f"Error getting songs from playlist {playlist_info.get('title')}: {str(e)}")
                    continue
            
            # Prefetch top results in the background with MEDIUM priority
            if all_songs:
                video_ids = [song.get('videoId') for song in all_songs[:3] if song.get('videoId')]
                if video_ids:
                    background_prefetch_audio_urls(video_ids, TaskPriority.MEDIUM)
            
            return all_songs[:limit]
        except Exception as featured_error:
            logger.error(f"Error using featured playlists approach: {str(featured_error)}")
            search_results = ytmusic.search("top hits", filter="songs", limit=limit)
            
            # Prefetch top results in the background with MEDIUM priority
            if search_results:
                video_ids = [song.get('videoId') for song in search_results[:3] if song.get('videoId')]
                if video_ids:
                    background_prefetch_audio_urls(video_ids, TaskPriority.MEDIUM)
            
            return search_results
    except Exception as e:
        logger.error(f"Error fetching trending songs: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get trending songs: {str(e)}")

@app.get("/featured")
def get_featured_playlists(limit: int = Query(10, description="Number of featured playlists to return")):
    try:
        logger.info("Fetching featured playlists...")
        
        # Get the home page content which contains featured playlists
        home_content = ytmusic.get_home()
        
        featured_playlists = []
        
        # Extract playlist information from home content
        for section in home_content:
            if 'contents' in section:
                for item in section['contents']:
                    # Check if it's a playlist
                    if 'playlistId' in item:
                        playlist_info = {
                            'playlistId': item['playlistId'],
                            'title': item.get('title', 'Unknown Playlist'),
                            'description': item.get('description', ''),
                            'thumbnails': item.get('thumbnails', []),
                            'author': item.get('author', {})
                        }
                        featured_playlists.append(playlist_info)
                        
                        # Break if we have enough playlists
                        if len(featured_playlists) >= limit:
                            break
            
            # Break if we have enough playlists
            if len(featured_playlists) >= limit:
                break
        
        return featured_playlists
    except Exception as e:
        logger.error(f"Error fetching featured playlists: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get featured playlists: {str(e)}")

@app.get("/task_stats")
def get_task_statistics():
    """Get current task priority statistics"""
    try:
        stats = priority_pool.get_stats()
        return {
            "task_statistics": stats,
            "total_tasks": sum(stats.values()),
            "priority_distribution": {
                "critical": f"{(stats['critical'] / max(sum(stats.values()), 1)) * 100:.1f}%",
                "high": f"{(stats['high'] / max(sum(stats.values()), 1)) * 100:.1f}%",
                "medium": f"{(stats['medium'] / max(sum(stats.values()), 1)) * 100:.1f}%",
                "low": f"{(stats['low'] / max(sum(stats.values()), 1)) * 100:.1f}%",
                "background": f"{(stats['background'] / max(sum(stats.values()), 1)) * 100:.1f}%"
            }
        }
    except Exception as e:
        logger.error(f"Error getting task statistics: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get task statistics: {str(e)}")

@app.get("/critical_prefetch")
def critical_prefetch_endpoint(video_ids: str = Query(..., description="Comma-separated video IDs")):
    """Prefetch audio URLs with critical priority for immediate playback"""
    try:
        video_id_list = [vid.strip() for vid in video_ids.split(",") if vid.strip()]
        if not video_id_list:
            return {"error": "No valid video IDs provided"}
        
        logger.info(f"Critical prefetch requested for {len(video_id_list)} videos")
        critical_prefetch_audio_urls(video_id_list)
        
        return {
            "message": f"Critical prefetch initiated for {len(video_id_list)} videos",
            "video_ids": video_id_list
        }
    except Exception as e:
        logger.error(f"Error in critical prefetch: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Critical prefetch failed: {str(e)}")

@app.get("/playlist")
def get_playlist_tracks(playlist_id: str = Query(..., description="YouTube Music playlist ID"), 
                       limit: int = Query(50, description="Number of tracks to return")):
    try:
        logger.info(f"Fetching playlist with ID: {playlist_id}")
        if playlist_id.startswith("RDCLAK"):
            featured_playlists = []
            home_content = ytmusic.get_home()
            matching_playlist = None
            for section in home_content:
                if 'contents' in section:
                    for item in section['contents']:
                        if 'playlistId' in item and item['playlistId'] == playlist_id:
                            matching_playlist = item
                            break
            if matching_playlist and 'title' in matching_playlist:
                search_results = ytmusic.search(matching_playlist['title'], filter="songs", limit=limit)
                
                # Prefetch top results in the background (never block the response)
                if search_results:
                    video_ids = [song.get('videoId') for song in search_results[:3] if song.get('videoId')]
                    if video_ids:
                        background_prefetch_audio_urls(video_ids)
                        
                return {
                    "playlistInfo": {
                        "title": matching_playlist.get('title', 'Radio Playlist'),
                        "description": matching_playlist.get('description', ''),
                        "thumbnails": matching_playlist.get('thumbnails', [])
                    },
                    "tracks": search_results
                }
            else:
                search_results = ytmusic.search("popular songs", filter="songs", limit=limit)
                
                # Prefetch top results in the background (never block the response)
                if search_results:
                    video_ids = [song.get('videoId') for song in search_results[:3] if song.get('videoId')]
                    if video_ids:
                        background_prefetch_audio_urls(video_ids)
                        
                return {
                    "playlistInfo": {
                        "title": "Popular Songs",
                        "description": "Popular songs collection"
                    },
                    "tracks": search_results
                }
        try:
            playlist = ytmusic.get_playlist(playlist_id, limit=limit)
            if 'tracks' in playlist:
                tracks = playlist['tracks']
                
                # Prefetch top results in the background (never block the response)
                if tracks:
                    video_ids = [song.get('videoId') for song in tracks[:3] if song.get('videoId')]
                    if video_ids:
                        background_prefetch_audio_urls(video_ids)
                        
                return playlist
            else:
                return playlist
        except Exception as e:
            logger.error(f"Error fetching playlist: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to get playlist: {str(e)}")
    except Exception as e:
        logger.error(f"Error in get_playlist_tracks: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get playlist tracks: {str(e)}")

@app.get("/yt_audio")
async def get_yt_audio(request: Request, video_id: str = Query(..., description="YouTube video ID")):
    """
    Get an audio stream with proper HTTP Range support for efficient seeking.
    """
    try:
        # Check cache first
        if video_id in audio_url_cache:
            audio_url, expire_timestamp, content_type = audio_url_cache[video_id]
            # If URL is still valid (not expired)
            if time.time() < expire_timestamp:
                logger.info(f"Using cached audio URL for {video_id}, expires in {int(expire_timestamp - time.time())} seconds")
            else:
                # URL expired, remove from cache
                del audio_url_cache[video_id]
                audio_url = None
                content_type = None
        else:
            audio_url = None
            content_type = None
        
        # If not in cache or expired, extract new URL with CRITICAL priority
        if audio_url is None:
            logger.info(f"Extracting new audio URL for {video_id} (CRITICAL priority)")
            url = f"https://www.youtube.com/watch?v={video_id}"
            
            ydl_opts = {
                'format': 'bestaudio/best',
                'quiet': False,
                'no_warnings': False,
                'noplaylist': True,
                'skip_download': True,
                'socket_timeout': 15,  # Add timeout for network operations
                # Prioritize faster formats with smaller file sizes
                'format_sort': ['asr', 'abr', 'size', 'quality'],
            }
            
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    
                    if not info:
                        logger.error("No info returned from yt-dlp")
                        return {"error": "Could not extract video information"}
                    
                    # Try direct URL first
                    if 'url' in info:
                        audio_url = info['url']
                        logger.info("Found direct URL in info dict")
                        
                        # Make a HEAD request to get content type
                        try:
                            head_response = requests.head(audio_url, timeout=5)
                            content_type = head_response.headers.get('Content-Type', 'audio/mpeg')
                        except Exception as e:
                            logger.warning(f"HEAD request failed: {str(e)}")
                            content_type = 'audio/mpeg'  # Default if HEAD request fails
                        
                        # Parse expiration time
                        expire_timestamp = parse_expire_from_url(audio_url)
                        
                        # Cache the URL
                        audio_url_cache[video_id] = (audio_url, expire_timestamp, content_type)
                        
                        logger.info(f"Cached audio URL for {video_id}, expires at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire_timestamp))}")
                        
                    else:
                        # Back to the old way if no direct URL
                        formats = info.get('formats', [])
                        if not formats:
                            return {"error": "No formats found", "url": url}
                        
                        # Try to find an audio format
                        audio_formats = [f for f in formats if f.get('acodec') != 'none']
                        
                        if not audio_formats:
                            # Fall back to any format if no audio formats are found
                            logger.warning("No audio formats found, using all formats")
                            audio_formats = formats
                        
                        # Sort by quality (prefer audio only, smaller file size, then by bitrate)
                        audio_formats.sort(key=lambda f: (
                            0 if f.get('vcodec') in (None, 'none') else 1,  # Prefer audio only
                            f.get('filesize') or float('inf'),  # Then prefer smaller file size
                            -(f.get('abr', 0) or 0)  # Then by audio bitrate (higher first)
                        ))
                        
                        if not audio_formats:
                            return {"error": "No formats available"}
                        
                        best_audio = audio_formats[0]
                        audio_url = best_audio.get('url')
                        
                        if not audio_url:
                            return {"error": "No URL found in best audio format"}
                        
                        logger.info(f"Selected format: {best_audio.get('format_id')}, filesize: {best_audio.get('filesize') or 'unknown'}")
                        
                        # Get content type
                        content_type = best_audio.get('mime_type', 'audio/mpeg').split(';')[0]
                        
                        # Parse expiration time
                        expire_timestamp = parse_expire_from_url(audio_url)
                        
                        # Cache the URL
                        audio_url_cache[video_id] = (audio_url, expire_timestamp, content_type)
                        
                        logger.info(f"Cached audio URL for {video_id}, expires at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire_timestamp))}")
            except Exception as yt_error:
                logger.error(f"Error extracting with yt-dlp: {str(yt_error)}")
                return {"error": f"Error extracting audio: {str(yt_error)}"}
        
        # Prepare headers for the request to YouTube
        headers = {}
        
        # Forward the Range header if present (critical for seeking)
        if "range" in request.headers:
            headers["Range"] = request.headers["range"]
            logger.info(f"Forwarding Range header: {headers['Range']}")
        
        # Add compression support
        headers["Accept-Encoding"] = "gzip, deflate"
        
        # Make the request to YouTube with increased timeout
        try:
            # Use session for connection pooling
            session = requests.Session()
            response = session.get(
                audio_url, 
                headers=headers, 
                stream=True, 
                timeout=15
            )
        except requests.exceptions.Timeout:
            logger.error(f"Timeout when requesting audio URL: {audio_url}")
            return {"error": "Timeout when requesting audio stream"}
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error: {str(e)}")
            return {"error": f"Error requesting audio stream: {str(e)}"}
        
        # Prepare response headers
        response_headers = {}
        
        # Forward important headers from YouTube's response
        important_headers = [
            "Content-Type", "Content-Length", "Content-Range", 
            "Accept-Ranges", "Content-Disposition", "Content-Encoding"
        ]
        
        for header in important_headers:
            if header in response.headers:
                response_headers[header] = response.headers[header]
        
        # Ensure Content-Type is set
        if "Content-Type" not in response_headers:
            response_headers["Content-Type"] = content_type
            
        # Set Content-Disposition
        response_headers["Content-Disposition"] = f'inline; filename="{video_id}.mp3"'
        
        # Add caching headers for better performance
        response_headers["Cache-Control"] = "max-age=3600"
        
        # Use a larger chunk size for faster streaming (64KB instead of 1KB)
        chunk_size = 65536  # 64KB chunks
        
        # Return the streaming response with the status code from YouTube
        return StreamingResponse(
            response.iter_content(chunk_size=chunk_size),
            status_code=response.status_code,
            headers=response_headers
        )
        
    except Exception as e:
        logger.error(f"Error in yt_audio: {str(e)}", exc_info=True)
        return {"error": f"Error streaming audio: {str(e)}"}

# Shutdown event handler to clean up resources
@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources on shutdown"""
    logger.info("Shutting down Neotune API...")
    try:
        # Shutdown priority thread pool
        priority_pool.shutdown()
        logger.info("Priority thread pool shutdown complete")
        
        # Shutdown legacy thread pools
        prefetch_thread_pool.shutdown(wait=True)
        download_thread_pool.shutdown(wait=True)
        logger.info("Legacy thread pools shutdown complete")
        
        # Clean up locks
        cleanup_locks()
        logger.info("Lock cleanup complete")
        
    except Exception as e:
        logger.error(f"Error during shutdown: {str(e)}")

@app.get("/download_audio")
async def download_audio(video_id: str = Query(..., description="YouTube video ID")):
    """
    Optimized endpoint for downloading audio files (not for streaming).
    This endpoint is optimized for download speed rather than streaming playback.
    """
    try:
        # Get audio URL (reusing existing function)
        audio_url, expire_timestamp, content_type = get_or_cache_audio_url(video_id)
        
        if not audio_url:
            return {"error": "Could not extract audio URL"}
            
        # Make request with optimized settings
        session = requests.Session()
        
        # Use a HEAD request to get content info
        head_response = session.head(
            audio_url, 
            timeout=10,
            headers={"Accept-Encoding": "gzip, deflate"}
        )
        
        # Check if the source supports range requests
        supports_ranges = "accept-ranges" in head_response.headers and head_response.headers["accept-ranges"] == "bytes"
        
        # Get response headers
        response_headers = {
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="{video_id}.mp3"',
            "Cache-Control": "max-age=3600"
        }
        
        # Forward content length if available
        if "content-length" in head_response.headers:
            response_headers["Content-Length"] = head_response.headers["content-length"]
            
        # If range requests supported, prepare for faster download
        if supports_ranges:
            # Use a custom streaming response generator for parallel range requests
            async def download_generator():
                content_length = int(head_response.headers.get("content-length", "0"))
                
                # Only use parallel downloads for files over 1MB
                if content_length > 1024 * 1024:
                    # Use 4MB chunks for parallel downloading
                    chunk_size = 4 * 1024 * 1024
                    chunk_count = (content_length + chunk_size - 1) // chunk_size
                    
                    # Limit chunks to 5 to avoid too many parallel requests
                    chunk_count = min(chunk_count, 5)
                    
                    # Define chunk ranges
                    ranges = []
                    for i in range(chunk_count):
                        start = i * chunk_size
                        end = min(start + chunk_size - 1, content_length - 1)
                        ranges.append((start, end))
                    
                    # Download chunks in parallel
                    responses = []
                    for start, end in ranges:
                        range_header = f"bytes={start}-{end}"
                        responses.append(session.get(
                            audio_url,
                            headers={"Range": range_header, "Accept-Encoding": "gzip, deflate"},
                            stream=True,
                            timeout=30
                        ))
                    
                    # Yield chunks in order
                    for resp in responses:
                        for chunk in resp.iter_content(65536):  # 64KB chunks
                            yield chunk
                            
                else:
                    # For smaller files, use a simple download
                    response = session.get(
                        audio_url, 
                        headers={"Accept-Encoding": "gzip, deflate"},
                        stream=True, 
                        timeout=30
                    )
                    
                    # Use larger chunk size for faster downloads
                    for chunk in response.iter_content(65536):  # 64KB chunks
                        yield chunk
            
            # Return streaming response with improved download performance
            return StreamingResponse(
                download_generator(),
                headers=response_headers
            )
            
        else:
            # Fallback to simple download if range requests not supported
            response = session.get(
                audio_url, 
                headers={"Accept-Encoding": "gzip, deflate"},
                stream=True, 
                timeout=30
            )
            
            # Return streaming response with 64KB chunks for better performance
            return StreamingResponse(
                response.iter_content(chunk_size=65536),  # 64KB chunks
                headers=response_headers
            )
            
    except Exception as e:
        logger.error(f"Error in download_audio: {str(e)}", exc_info=True)
        return {"error": f"Error downloading audio: {str(e)}"}

@app.get("/youtube-dl-helper.js")
async def youtube_dl_helper():
    """
    Serve a JavaScript helper that can extract YouTube audio streams in the browser
    """
    js_content = """
// YouTube Player Extraction Helper
const YouTubeHelper = {
    // Extract audio streams directly from YouTube
    extractAudioStreams: async function(videoId) {
        try {
            // First try to get the video page
            const videoUrl = `https://www.youtube.com/watch?v=${videoId}`;
            const response = await fetch(videoUrl);
            const html = await response.text();
            
            // Look for the ytInitialPlayerResponse in the page
            const playerResponseMatch = html.match(/ytInitialPlayerResponse\\s*=\\s*(\\{.+?\\});\\s*(?:var\\s+meta|<\\/script>)/);
            if (!playerResponseMatch) {
                throw new Error('Could not find player response data');
            }
            
            try {
                const playerResponse = JSON.parse(playerResponseMatch[1]);
                const streamingData = playerResponse.streamingData;
                
                if (!streamingData) {
                    throw new Error('No streaming data found');
                }
                
                // Extract audio formats
                const formats = [
                    ...(streamingData.adaptiveFormats || []),
                    ...(streamingData.formats || [])
                ];
                
                // Filter for audio formats
                const audioFormats = formats.filter(format => {
                    return format.mimeType && format.mimeType.includes('audio');
                });
                
                // Get video details
                const videoDetails = playerResponse.videoDetails || {};
                
                return {
                    title: videoDetails.title || 'Unknown',
                    formats: audioFormats,
                    thumbnail: videoDetails.thumbnail ? 
                        videoDetails.thumbnail.thumbnails[videoDetails.thumbnail.thumbnails.length - 1].url : '',
                    duration: videoDetails.lengthSeconds || 0
                };
                
            } catch (parseError) {
                console.error('Error parsing player data:', parseError);
                throw new Error('Failed to parse player data');
            }
        } catch (error) {
            console.error('Error extracting streams:', error);
            throw error;
        }
    }
};
    """
    return JSONResponse(content={"code": js_content})

@app.get("/audio_fallback")
async def audio_fallback(request: Request, video_id: str = Query(..., description="YouTube video ID")):
    """
    Fallback endpoint that streams audio directly using a different approach
    """
    try:
        # Check cache first
        if video_id in audio_url_cache:
            audio_url, expire_timestamp, content_type = audio_url_cache[video_id]
            # If URL is still valid (not expired)
            if time.time() < expire_timestamp:
                logger.info(f"Using cached audio URL for fallback {video_id}, expires in {int(expire_timestamp - time.time())} seconds")
            else:
                # URL expired, remove from cache
                del audio_url_cache[video_id]
                audio_url = None
                content_type = None
        else:
            audio_url = None
            content_type = None
        
        # If not in cache or expired, extract new URL
        if audio_url is None:
            logger.info(f"Audio fallback for ID: {video_id}")
            url = f"https://www.youtube.com/watch?v={video_id}"
            
            # Use the same approach as the main endpoint but with different options
            ydl_opts = {
                'format': 'bestaudio/best',
                'quiet': False,
                'no_warnings': False,
                'noplaylist': True,
                'skip_download': True,
                'socket_timeout': 15,  # Add timeout for network operations
            }
            
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    
                    if not info:
                        return {"error": "Could not extract video information"}
                    
                    # Try direct URL first
                    if 'url' in info:
                        audio_url = info['url']
                        logger.info("Found direct URL in fallback")
                        
                        # Make a HEAD request to get content type
                        try:
                            head_response = requests.head(audio_url, timeout=5)
                            content_type = head_response.headers.get('Content-Type', 'audio/mpeg')
                        except Exception as e:
                            logger.warning(f"HEAD request failed in fallback: {str(e)}")
                            content_type = 'audio/mpeg'  # Default if HEAD request fails
                        
                        # Parse expiration time
                        expire_timestamp = parse_expire_from_url(audio_url)
                        
                        # Cache the URL
                        audio_url_cache[video_id] = (audio_url, expire_timestamp, content_type)
                        
                        logger.info(f"Cached fallback audio URL for {video_id}, expires at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire_timestamp))}")
                        
                    else:
                        # Process formats if no direct URL
                        formats = info.get('formats', [])
                        if formats:
                            # Try to find an audio format
                            audio_formats = [f for f in formats if f.get('acodec') != 'none']
                            
                            if audio_formats:
                                audio_formats.sort(key=lambda f: -(f.get('abr', 0) or 0))
                                best_audio = audio_formats[0]
                                audio_url = best_audio.get('url')
                                
                                if audio_url:
                                    # Get content type
                                    content_type = best_audio.get('mime_type', 'audio/mpeg').split(';')[0]
                                    
                                    # Parse expiration time
                                    expire_timestamp = parse_expire_from_url(audio_url)
                                    
                                    # Cache the URL
                                    audio_url_cache[video_id] = (audio_url, expire_timestamp, content_type)
                                    
                                    logger.info(f"Cached fallback audio URL for {video_id}, expires at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire_timestamp))}")
            except Exception as yt_error:
                logger.error(f"Error extracting with yt-dlp in fallback: {str(yt_error)}")
                return {"error": f"Error extracting audio in fallback: {str(yt_error)}", "video_id": video_id}
        
        if not audio_url:
            return {"error": "No suitable audio URL found", "video_id": video_id}
        
        # Prepare headers for the request to YouTube
        headers = {}
        
        # Forward the Range header if present (critical for seeking)
        if "range" in request.headers:
            headers["Range"] = request.headers["range"]
            logger.info(f"Forwarding Range header to fallback: {headers['Range']}")
        
        # Make the request to YouTube
        try:
            response = requests.get(audio_url, headers=headers, stream=True, timeout=10)
        except requests.exceptions.Timeout:
            logger.error(f"Timeout when requesting fallback audio URL: {audio_url}")
            return {"error": "Timeout when requesting fallback audio stream", "video_id": video_id}
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error in fallback: {str(e)}")
            return {"error": f"Error requesting fallback audio stream: {str(e)}", "video_id": video_id}
        
        # Prepare response headers
        response_headers = {}
        
        # Forward important headers from YouTube's response
        important_headers = [
            "Content-Type", "Content-Length", "Content-Range", 
            "Accept-Ranges", "Content-Disposition"
        ]
        
        for header in important_headers:
            if header in response.headers:
                response_headers[header] = response.headers[header]
        
        # Ensure Content-Type is set
        if "Content-Type" not in response_headers:
            response_headers["Content-Type"] = content_type
            
        # Set Content-Disposition
        response_headers["Content-Disposition"] = f'inline; filename="{video_id}_fallback.mp3"'
        
        # Return the streaming response with the status code from YouTube
        return StreamingResponse(
            response.iter_content(chunk_size=1024),
            status_code=response.status_code,
            headers=response_headers
        )
        
    except Exception as e:
        logger.error(f"Error in audio_fallback: {str(e)}", exc_info=True)
        return {"error": str(e), "video_id": video_id}

if __name__ == "__main__":
    import uvicorn
    import multiprocessing
    import platform  # Import the platform module to check the OS

    # Default to a single worker
    workers = 1

    # On non-Windows systems, calculate the optimal number of workers
    if platform.system() != "Windows":
        workers = min(4, multiprocessing.cpu_count() + 1)

    # The logic to use an import string for multiple workers is still good practice
    if workers > 1:
        # For multiple workers, we need to use an import string
        uvicorn.run(
            "main:app",  # Use import string instead of app instance
            host="0.0.0.0",
            port=8000,
            workers=workers,
            timeout_keep_alive=65,
            log_level="info"
        )
    else:
        # For a single worker (or on Windows), we can use the app instance directly
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=8000,
            timeout_keep_alive=65,
            log_level="info"
        )