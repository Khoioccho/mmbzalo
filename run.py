"""
MMBZalo — Entry Point
Run: python run.py
"""

import uvicorn

if __name__ == "__main__":
    print()
    print("  +==========================================+")
    print("  |       MMBZalo Automation Tool v0.2       |")
    print("  |  Dashboard -> http://localhost:8000      |")
    print("  +==========================================+")
    print()

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=["app", "frontend"],
        log_level="info",
    )
