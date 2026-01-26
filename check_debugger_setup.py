"""
CHECK DEBUGGER SETUP

This script checks if the Python debugger packages are properly installed
and can help diagnose PyCharm debugger connection issues.
"""

import sys
import importlib.util


def check_package(package_name: str) -> bool:
    """Check if a package is installed."""
    spec = importlib.util.find_spec(package_name)
    return spec is not None


def get_package_version(package_name: str) -> str:
    """Get the version of an installed package."""
    try:
        module = importlib.import_module(package_name)
        if hasattr(module, '__version__'):
            return module.__version__
        return "installed (version unknown)"
    except ImportError:
        return "not installed"


def main():
    """Run diagnostic checks for PyCharm debugger setup."""
    print("=" * 60)
    print("PyCharm Debugger Diagnostic Check")
    print("=" * 60)
    print()
    
    print(f"Python Version: {sys.version}")
    print(f"Python Executable: {sys.executable}")
    print()
    
    print("Checking debugger packages:")
    print("-" * 60)
    
    packages_to_check = [
        'pydevd',
        'pydevd_pycharm',
        'debugpy'
    ]
    
    all_installed = True
    for package in packages_to_check:
        installed = check_package(package)
        version = get_package_version(package)
        status = "[OK]" if installed else "[MISSING]"
        print(f"{status} {package:20s} - {version}")
        if not installed:
            all_installed = False
    
    print()
    print("=" * 60)
    
    print("NOTE: PyCharm bundles its own debugger, so these packages")
    print("      may not be required for PyCharm debugging.")
    print()
    print("If you're experiencing 'Connection to Python debugger failed' errors:")
    print()
    print("SOLUTION 1: Clear PyCharm Caches (Most Common Fix)")
    print("  File -> Invalidate Caches... -> Invalidate and Restart")
    print()
    print("SOLUTION 2: Check Debugger Port Settings")
    print("  File -> Settings -> Build, Execution, Deployment -> Python Debugger")
    print("  - Try changing the port (default is usually 5005)")
    print("  - Ensure 'Attach to subprocess automatically' is checked if needed")
    print()
    print("SOLUTION 3: Verify Python Interpreter")
    print("  File -> Settings -> Project -> Python Interpreter")
    print(f"  Ensure: {sys.executable}")
    print()
    print("SOLUTION 4: Check Firewall/Antivirus")
    print("  Temporarily disable to test if it's blocking the debugger connection")
    print()
    print("SOLUTION 5: Restart PyCharm Completely")
    print("  Close all PyCharm windows and restart")
    print()
    print("SOLUTION 6: Check for Multiple Network Adapters")
    print("  PyCharm may be trying to bind to the wrong network interface")
    print("  Try: File -> Settings -> Build, Execution, Deployment -> Python Debugger")
    print("       Set 'Gevent compatible' or change 'Attach subprocess automatically'")
    
    print("=" * 60)


if __name__ == "__main__":
    main()
