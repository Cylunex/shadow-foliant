#!/bin/bash
# 网络出口长跑监控 — 每 30s 探 4 家数据源,记 RTT + 失败码。
# 用途:任务卡死时回头对账"那一刻网络通不通",定性"机房间歇性问题" vs "代码层卡死"。
#
# 部署(生产服务器):
#   nohup bash scripts/net_watch.sh >> /var/log/net_watch.log 2>&1 &
#   disown
# 看实时:
#   tail -f /var/log/net_watch.log
# 找异常:
#   grep -E 'FAIL|code=000|total=[5-9]\.|total=[1-9][0-9]' /var/log/net_watch.log
# 对账任务挂的时刻:
#   awk '/2026-06-30 09:0[0-9]/' /var/log/net_watch.log

set -u
ENDPOINTS=(
  "tencent|https://qt.gtimg.cn/q=sh600519"
  "sina|https://quotes.sina.cn/cn/api/jsonp_v2.php/var=/CN_MarketData.getKLineData?symbol=sh600519&scale=240&datalen=3"
  "eastmoney|https://push2.eastmoney.com/api/qt/ulist.np/get?secids=1.600519&fields=f2"
  "iwencai|https://www.iwencai.com/customized/chart/get-robot-data"
)

while true; do
  ts=$(date '+%Y-%m-%d %H:%M:%S')
  for ep in "${ENDPOINTS[@]}"; do
    name=${ep%%|*}
    url=${ep#*|}
    out=$(curl -m8 -sS -o /dev/null \
      -w "code=%{http_code} dns=%{time_namelookup}s connect=%{time_connect}s tls=%{time_appconnect}s total=%{time_total}s" \
      "$url" -A "Mozilla/5.0" 2>&1) || out="FAIL: $out"
    echo "$ts $name $out"
  done
  sleep 30
done
