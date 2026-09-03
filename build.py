import os
import subprocess
import sys

def run_build(name, noconsole=False):
    print(f"\n[{name}] Starting build {'(without console)' if noconsole else '(with console)'}...")
    
    command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", name
    ]
    
    if noconsole:
        command.append("--noconsole")
        
    command.append("main.py")
    
    result = subprocess.run(command)
    
    if result.returncode != 0:
        print(f"\n[{name}] Build failed.")
        sys.exit(result.returncode)
    else:
        print(f"[{name}] Build successful.")

def build():
    print("Checking dependencies...")
    
    try:
        import PyInstaller
    except ImportError:
        print("PyInstaller is not installed. Please install it using: pip install pyinstaller")
        sys.exit(1)

    # 1. Сборка консольной версии
    run_build("MicroBackUp", noconsole=False)
    
    # 2. Сборка фоновой версии (без консоли)
    run_build("MicroBackUp-bg", noconsole=True)
    
    print("\nAll builds completed successfully!")
    print("Executables (MicroBackUp.exe and MicroBackUp-bg.exe) can be found in the 'dist' folder.")

if __name__ == "__main__":
    build()
