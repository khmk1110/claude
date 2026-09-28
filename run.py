"""起動: python run.py → ブラウザで http://127.0.0.1:5000 を開く"""
from payroll import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
