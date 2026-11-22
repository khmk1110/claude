class NoStoreHtmlMiddleware:
    """画面（HTML）をブラウザに保存させない。古い画面からの送信（CSRFエラー）や、ログアウト後に戻るボタンで
    給与などの画面が見えてしまうことを防ぐ。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.get("Content-Type", "").startswith("text/html"):
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response["Pragma"] = "no-cache"
        return response
