#!/usr/bin/env python3
"""
Quick health check for Castopia Bot
Verifies all dependencies and configuration before deployment
"""

import sys
import os
from pathlib import Path

def check_python_version():
    """Check Python version >= 3.10"""
    if sys.version_info < (3, 10):
        print(f"❌ Python 3.10+ required, got {sys.version_info.major}.{sys.version_info.minor}")
        return False
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}")
    return True

def check_dependencies():
    """Check all required packages"""
    required = ['discord', 'aiogram', 'aiohttp', 'bs4', 'lxml', 'dotenv']
    missing = []
    
    for package in required:
        try:
            __import__(package if package != 'bs4' else 'bs4')
            print(f"✓ {package}")
        except ImportError:
            print(f"❌ {package} missing")
            missing.append(package)
    
    if missing:
        print(f"\nInstall with: pip install {' '.join(missing)}")
        return False
    return True

def check_env_file():
    """Check .env configuration"""
    env_path = Path('.env')
    
    if not env_path.exists():
        print("❌ .env file not found")
        print("  Copy from .env.example: cp .env.example .env")
        return False
    
    print(f"✓ .env exists")
    
    # Check critical variables
    with open(env_path) as f:
        content = f.read()
    
    required_vars = [
        'DISCORD_BOT_TOKEN',
        'TELEGRAM_BOT_TOKEN',
        'WIKI_BASE_URL'
    ]
    
    missing_vars = [v for v in required_vars if v not in content]
    
    if missing_vars:
        print(f"❌ Missing variables: {', '.join(missing_vars)}")
        return False
    
    print(f"✓ All required variables set")
    
    # Check token format
    if 'your_token_here' in content or 'token' in content.lower() and '=' in content:
        actual_tokens = sum(1 for line in content.split('\n') 
                          if line.startswith(('DISCORD_BOT_TOKEN=', 'TELEGRAM_BOT_TOKEN=')) 
                          and line.split('=')[1].strip() 
                          and 'your_token_here' not in line)
        if actual_tokens < 2:
            print("⚠️  Some tokens may not be configured")
            return False
    
    return True

def check_module_structure():
    """Check project structure"""
    required_files = [
        'dsc/bot.py',
        'tg/bot.py',
        'cogs/page_parsing.py',
        'cogs/dsc.py',
        'cogs/tg.py',
        'familiarbot/bot.py',
        'familiarbot/moderator.py',
        'requirements.txt',
    ]
    
    missing = [f for f in required_files if not Path(f).exists()]
    
    if missing:
        print(f"❌ Missing files: {', '.join(missing)}")
        return False
    
    print(f"✓ All module files present")
    return True

def check_imports():
    """Try importing main modules"""
    try:
        import cogs.page_parsing
        print("✓ cogs.page_parsing imports")
    except Exception as e:
        print(f"❌ cogs.page_parsing: {e}")
        return False
    
    try:
        import cogs.dsc
        print("✓ cogs.dsc imports")
    except Exception as e:
        print(f"❌ cogs.dsc: {e}")
        return False
    
    try:
        import cogs.tg
        print("✓ cogs.tg imports")
    except Exception as e:
        print(f"❌ cogs.tg: {e}")
        return False
    
    return True

def main():
    print("=" * 50)
    print("  Castopia Bot - Health Check")
    print("=" * 50)
    print()
    
    checks = [
        ("Python Version", check_python_version),
        ("Dependencies", check_dependencies),
        ("Configuration (.env)", check_env_file),
        ("Project Structure", check_module_structure),
        ("Module Imports", check_imports),
    ]
    
    results = []
    
    for name, check_func in checks:
        print(f"\n[{name}]")
        try:
            result = check_func()
            results.append((name, result))
        except Exception as e:
            print(f"❌ Error: {e}")
            results.append((name, False))
    
    print("\n" + "=" * 50)
    print("Summary:")
    print("=" * 50)
    
    for name, result in results:
        status = "✓" if result else "❌"
        print(f"{status} {name}")
    
    all_pass = all(r for _, r in results)
    
    print()
    if all_pass:
        print("✅ All checks passed! Ready to run:")
        print("  python dsc/bot.py   (Discord bot)")
        print("  python tg/bot.py    (Telegram bot)")
        return 0
    else:
        print("❌ Some checks failed. Please fix issues above.")
        return 1

if __name__ == '__main__':
    sys.exit(main())
