from analysis.weekly_news import relevant_official_news


def test_weekly_news_requires_holding_match_and_official_link():
    holdings = [{"code": "601899", "name": "紫金矿业"}]
    news = [
        {"title": "电影延期上映", "url": "https://www.sse.com.cn/a"},
        {"title": "紫金矿业股价预测", "url": "https://example.com/a"},
        {"title": "紫金矿业公告", "url": "https://www.cninfo.com.cn/a", "time": "2026-09-20 12:00"},
        {"title": "紫金矿业公告", "url": "https://www.cninfo.com.cn/a"},
    ]
    result = relevant_official_news(news, holdings)
    assert len(result) == 1
    assert "紫金矿业公告" in result[0]
    assert "https://www.cninfo.com.cn/a" in result[0]
