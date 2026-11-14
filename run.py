"""Start the Millennium Mutual money ledger.

    pip install -r requirements.txt
    python run.py

Then open http://127.0.0.1:8000 (it opens automatically).
"""
import threading
import webbrowser

import uvicorn

HOST, PORT = "127.0.0.1", 8000

if __name__ == "__main__":
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run("backend.app:app", host=HOST, port=PORT, reload=False)
