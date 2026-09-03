import os
import shutil
import subprocess
import sys

def _clean_build_dirs():
    root = os.path.dirname(os.path.abspath(__file__))
    for name in ("build", "dist"):
        path = os.path.join(root, name)
        if os.path.isdir(path):
            print(f"Cleaning {path}/")
            shutil.rmtree(path, ignore_errors=True)
    # Remove stale .spec files from previous PyInstaller runs.
    for entry in os.listdir(root):
        if entry.endswith(".spec"):
            spec = os.path.join(root, entry)
            print(f"Removing stale spec: {entry}")
            try:
                os.remove(spec)
            except OSError:
                pass

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

def ensure_dependencies():
    print("Checking dependencies...")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        req_build = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements-build.txt")
        print("PyInstaller is not installed. Installing build dependencies from requirements-build.txt...")
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_build])
        if result.returncode != 0:
            print("Failed to install dependencies.")
            sys.exit(result.returncode)


def build():
    ensure_dependencies()
    _clean_build_dirs()

    # 1. Сборка консольной версии
    run_build("MicroBackUp", noconsole=False)
    
    # 2. Сборка фоновой версии (без консоли)
    run_build("MicroBackUp-bg", noconsole=True)
    
    print("\nAll builds completed successfully!")
    print("Executables (MicroBackUp.exe and MicroBackUp-bg.exe) can be found in the 'dist' folder.")

if __name__ == "__main__":
    build()
