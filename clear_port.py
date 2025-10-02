#!/usr/bin/env python
"""
Port Cleanup Utility

A standalone script to find and terminate a process that is blocking a specific port.
This is useful for cleaning up "zombie" server processes that did not shut down correctly.

Usage:
    python clear_port.py
    python clear_port.py --port 8000
"""
import subprocess
import sys
import re
import platform
import argparse
import socket
import time

def find_and_kill_process_on_port(port: int):
    """
    Finds and offers to kill the process using a specific port.
    This function is cross-platform and works on Windows, Linux, and macOS.
    """
    print(f"Checking port {port}...")

    # --- Step 1: Check if the port is actually in use ---
    # We try to connect to the port instead of binding to it.
    # This is a more reliable way to see if a service is actively listening.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(('127.0.0.1', port)) != 0:
            print(f"Success: Port {port} is already free.")
            return

    print(f"Port {port} is currently in use by another process.")
    pid = None
    command = ""
    os_name = platform.system()

    try:
        # --- Step 2: Find the Process ID (PID) using OS-specific commands ---
        if os_name == "Windows":
            command = f'netstat -ano | findstr ":{port}"'
            result = subprocess.check_output(command, shell=True, text=True, stderr=subprocess.DEVNULL)
            # Search for a line with the port in a LISTENING state
            match = re.search(r'LISTENING\s+(\d+)', result)
            if match:
                pid = match.group(1)
        elif os_name in ["Linux", "Darwin"]:  # Darwin is macOS
            command = f'lsof -ti tcp:{port}'
            result = subprocess.check_output(command, shell=True, text=True, stderr=subprocess.DEVNULL)
            pid = result.strip().split('\n')[0] # Take the first PID if multiple are returned

        if not pid:
            print(f"Could not automatically find the process ID using port {port}.")
            print("The process might be running under a different user or has already closed.")
            return

        # --- Step 3: Prompt the user for confirmation ---
        choice = input(f"A process with PID {pid} is using port {port}. Do you want to terminate it? (y/n): ").lower()

        if choice == 'y':
            print(f"Terminating process {pid}...")
            kill_command = ""
            if os_name == "Windows":
                kill_command = f"taskkill /PID {pid} /F"
            else:  # Linux/macOS
                kill_command = f"kill -9 {pid}"

            # --- Step 4: Execute the kill command ---
            subprocess.run(kill_command, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"Process {pid} terminated successfully.")
            # Give the OS a moment to release the port
            time.sleep(1)
            print(f"Port {port} should now be free.")
        else:
            print("Aborted. No action was taken.")

    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"Could not find or terminate the process on port {port}.")
        print(f"The command used was: '{command}'")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

def main():
    """Parses command-line arguments and runs the main logic."""
    parser = argparse.ArgumentParser(
        description="Find and kill a process running on a specific port.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        '-p', '--port',
        type=int,
        default=8000,
        help="The port number to check and clear.\nDefault is 8000."
    )
    args = parser.parse_args()
    find_and_kill_process_on_port(args.port)

if __name__ == "__main__":
    main()
