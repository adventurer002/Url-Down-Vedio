"""Run the isolated anonymous Douyin browser feasibility probe from backend/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.modules.video.parsers.douyin_browser import main

if __name__ == "__main__":
    main()
