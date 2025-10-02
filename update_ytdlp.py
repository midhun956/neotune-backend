#!/usr/bin/env python
"""
Script to automatically update yt-dlp to the latest version
"""
import subprocess
import sys
import os

def update_ytdlp():
    """Update yt-dlp to the latest version"""
    print("Checking for yt-dlp updates...")
    
    try:
        # First try to update using pip
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"], 
                      check=True)
        print("yt-dlp successfully updated via pip!")
        
        # Also try the self-update mechanism
        try:
            subprocess.run([sys.executable, "-m", "yt_dlp", "-U"], 
                          check=True)
            print("yt-dlp self-update completed!")
        except Exception as e:
            print(f"Self-update mechanism warning (not critical): {str(e)}")
        
        # Check the installed version
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "yt-dlp"],
            capture_output=True,
            text=True,
            check=True
        )
        
        for line in result.stdout.split('\n'):
            if line.startswith('Version:'):
                version = line.split('Version:')[1].strip()
                print(f"Current yt-dlp version: {version}")
                break
                
        print("yt-dlp update completed successfully!")
        return True
        
    except Exception as e:
        print(f"Error updating yt-dlp: {str(e)}")
        return False

if __name__ == "__main__":
    update_ytdlp() 