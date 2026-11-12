"""Evidence-bearing summaries for long weekly archive deliveries."""
import re

from notify.stock_report import report, text
from notify.report_details import history_reference


def archive_summary(content):
    lines = [line.strip() for line in str(content).splitlines() if line.strip()]
    if not lines:
        return report("周报", "", [("报告状态", ["本次无可用正文。"])])
    date = re.search(r"\d{4}-\d{2}-\d{2}", lines[0])
    as_of = date.group() if date else "日期见原稿"
    rows = []
    if "AI 推荐评估周报" in lines[0]:
        rows.extend(text(line, 120) for line in lines[1:] if line.startswith(("样本:", "口径：")))
        group = ""
        groups = 0
        for line in lines:
            if line.startswith("=== AI 推荐评估 ·"):
                break  # Main source groups already describe the complete sample.
            if line.startswith("["):
                groups += 1
                if groups > 4:
                    rows.append('其余来源分组详见完整报告。')
                    break
                group = text(line, 45)
                rows.append(group)
            elif group and line.startswith(("样本盈利占比", "盈亏比:")):
                rows.append(text(line, 100 if line.startswith('样本盈利占比') else 65))
        label = "推荐周报"
    elif "策略" in lines[0] and "回测" in lines[0]:
        rows.extend(text(line, 120) for line in lines if line.startswith("股票池:"))
        for line in lines:
            match = re.match(r"(\d+)\.\s+(.+?)\s+胜率=(\S+).*?avg_ret=(\S+).*?触发(\d+)次", line)
            if match and int(match[1]) <= 5:
                small = '（小样本）' if '小样本' in line else ''
                rows.append(f'{match[1]}. {text(match[2], 14)} 胜率{match[3]} 均回报{match[4]} 样本{match[5]}次{small}')
            elif line.startswith('纪律收益='):
                rows.append(text(line, 85))
            elif line.startswith('🧬 进化 vs'):
                rows.append(text(line, 175))
        label = "策略周报"
    else:
        rows = [text(line, 100) for line in lines if not line.startswith(("━━", "==="))][:5]
        return '\n'.join(rows + [history_reference()])
    return report(label, as_of, [("结果与口径", rows)], [history_reference()])
