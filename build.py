import os
import subprocess
import sys

def build():
    print("Starting build process with PyInstaller...")
    
    # Check if pyinstaller is installed
    try:
        import PyInstaller
    except ImportError:
        print("PyInstaller is not installed. Please install it using: pip install pyinstaller")
        sys.exit(1)

    # Command to run PyInstaller
    command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "MicroBackUp",
        "main.py"
    ]

    print(f"Running command: {' '.join(command)}")
    
    result = subprocess.run(command)
    
    if result.returncode == 0:
        print("\nBuild successful!")
        print("Executable can be found in the 'dist' folder.")
    else:
        print("\nBuild failed.")
        sys.exit(result.returncode)

if __name__ == "__main__":
    build()
