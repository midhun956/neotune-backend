#!/usr/bin/env python3
"""
Simple script to find your computer's IP address for Android app configuration
"""
import socket
import subprocess
import sys
import platform

def get_local_ip():
    """Get the local IP address of this computer"""
    try:
        # Create a socket to get the local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception as e:
        print(f"Error getting local IP: {e}")
        return None

def get_all_ips():
    """Get all IP addresses on this computer"""
    ips = []
    try:
        # Get all network interfaces
        for interface in socket.if_nameindex():
            try:
                # Get IP for this interface
                ip = socket.gethostbyname(socket.gethostname())
                if ip not in ips:
                    ips.append(ip)
            except:
                continue
    except:
        pass
    
    # Also try the socket method
    local_ip = get_local_ip()
    if local_ip and local_ip not in ips:
        ips.append(local_ip)
    
    return ips

def main():
    print("=== Neotune Backend IP Configuration ===")
    print()
    
    # Get local IP
    local_ip = get_local_ip()
    if local_ip:
        print(f"✅ Primary IP Address: {local_ip}")
        print(f"   Backend URL: http://{local_ip}:8000")
        print()
        
        # Show how to update the Android app
        print("📱 To update your Android app, change this line in Config.kt:")
        print(f'   const val BACKEND_BASE_URL = "http://{local_ip}:8000"')
        print()
    else:
        print("❌ Could not determine primary IP address")
        print()
    
    # Get all IPs
    all_ips = get_all_ips()
    if len(all_ips) > 1:
        print("🔍 All available IP addresses:")
        for ip in all_ips:
            print(f"   - http://{ip}:8000")
        print()
    
    # Platform-specific commands
    print("🔧 Manual IP lookup commands:")
    if platform.system() == "Windows":
        print("   Windows: ipconfig")
    elif platform.system() == "Darwin":  # macOS
        print("   macOS: ifconfig")
    else:  # Linux
        print("   Linux: ip addr show")
    print()
    
    print("💡 Tips:")
    print("   - Make sure your Android device and computer are on the same network")
    print("   - Check that your firewall allows connections on port 8000")
    print("   - The backend server must be running for the app to work")
    print()
    
    if local_ip:
        print("🚀 Ready to run the backend:")
        print("   cd backend")
        print("   python run.py")
        print()
        print(f"   Then test with: http://{local_ip}:8000")

if __name__ == "__main__":
    main()

