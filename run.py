#!/usr/bin/env python
"""
Neotune API Server Launcher
This script updates yt-dlp to the latest version before starting the server
"""
import subprocess
import sys
import os
import signal
import time
from update_ytdlp import update_ytdlp

def handle_signal(sig, frame):
    """Handle termination signals gracefully"""
    print("\nShutting down Neotune API Server...")
    sys.exit(0)

def main():
    """Main function to run the server with yt-dlp updates"""
    print("=== Neotune API Server Launcher ===")
    
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    # Update yt-dlp first
    print("\nStep 1: Updating yt-dlp to the latest version...")
    update_ytdlp()
    
    # Start the server
    print("\nStep 2: Starting Neotune API Server...")
    try:
        # Use this script's directory as the working directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        os.chdir(script_dir)
        
        # Run the main.py module with subprocess
        server_process = subprocess.Popen([sys.executable, "main.py"])
        
        # Wait for the server process to complete
        while server_process.poll() is None:
            time.sleep(1)
            
        # Check if the server exited with an error
        if server_process.returncode != 0:
            print(f"Server exited with error code {server_process.returncode}")
            return server_process.returncode
            
    except KeyboardInterrupt:
        print("\nReceived keyboard interrupt. Shutting down...")
        if 'server_process' in locals() and server_process.poll() is None:
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()
    except Exception as e:
        print(f"Error starting server: {str(e)}")
        return 1
        
    return 0

if __name__ == "__main__":
    sys.exit(main()) 