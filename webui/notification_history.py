"""Owner/admin views of the existing encrypted notification archive."""
from html import escape
from urllib.parse import urlencode

from fastapi import Query
from fastapi.responses import HTMLResponse


def _page(title, content):
    return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<title>{escape(title)}</title><style>body{{max-width:960px;margin:24px auto;'
                        'padding:0 16px;font:16px/1.7 system-ui}}pre{white-space:pre-wrap;'
                        'overflow-wrap:anywhere}a{color:#2767be}article{border-bottom:1px solid #ddd;'
                        'padding:12px 0}</style>' + content + '</html>',
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff"})


def register_notification_history(app):
    @app.get("/notifications", response_class=HTMLResponse)
    def history(from_date: str | None = None, to_date: str | None = None,
                offset: int = Query(0, ge=0)):
        from application.message_archive import MessageArchiveService
        service = MessageArchiveService()
        try:
            rows = service.list_messages(from_date=from_date, to_date=to_date, offset=offset, limit=50)
        except ValueError:
            return HTMLResponse("日期格式应为 YYYY-MM-DD", status_code=400)
        content = '<h1>通知记录</h1><p><a href="./">返回 Foliant</a></p>'
        content += ('<form>开始日期 <input type="date" name="from_date" value="' + escape(from_date or "")
                    + '"> 结束日期 <input type="date" name="to_date" value="' + escape(to_date or "")
                    + '"><button>查询</button></form>')
        for row in rows:
            detail = service.detail(row["message_id"])
            if detail:
                content += (f'<article><a href="notifications/{row["message_id"]}">'
                            f'{escape(detail["title"])}</a><br>{escape(str(row["generated_at"]))}'
                            f' · {escape(row["status"])}</article>')
        if len(rows) == 50:
            params = {"offset": offset + len(rows)}
            params.update({k: v for k, v in {"from_date": from_date, "to_date": to_date}.items() if v})
            content += '<a href="?' + escape(urlencode(params)) + '">下一页</a>'
        return _page("通知记录", content + ("" if rows else "<p>当前日期范围没有通知。</p>"))

    @app.get("/notifications/{message_id}", response_class=HTMLResponse)
    def detail(message_id: str):
        from application.message_archive import MessageArchiveService
        try:
            row = MessageArchiveService().detail(message_id)
        except ValueError:
            row = None
        if row is None:
            return HTMLResponse("未找到通知", status_code=404)
        content = (f'<h1>{escape(row["title"])}</h1><p><a href="../notifications">返回通知记录</a></p>'
                   '<h2>完整报告</h2><pre>' + escape(row["original_body"]) + '</pre>')
        for delivery in row["deliveries"]:
            content += ('<h2>' + escape(delivery["channel"]) + ' 投递正文</h2><p>'
                        + escape(delivery["status"]) + '</p><pre>'
                        + escape(delivery["final_body"]) + '</pre>')
        return _page(row["title"], content)
