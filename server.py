# -*- coding: utf-8 -*-
"""
Antigravity (AGY) 本地全中文实时任务进度、高精度 ETA、上下文与 Token 监控服务 (v2.1)
- 支持 --mcp 模式：通过工作区插件 (.agents/plugins/ag-monitor/mcp_config.json) 随 AGY 自动启动与自动重连，零 Token 注入开销
- 高精度多级 ETA 引擎：
  1) 自动追踪命令行重定向外部日志 (*.stdout.txt / *.log) 与 PowerShell foreach 多轮种子循环
  2) 同目录已完成日志经验步骤曲线校准 (Step Empirical Profile Calibration)
  3) 时间戳增量速率推算 (Delta-Rate，消除断点续传与启动加载偏差)
  4) tqdm 进度条与脚本原生显式 ETA 实时扣减年龄
  5) 自动识别服务重启中断与 schedule 定时器提前唤醒，杜绝僵尸“运行中”任务
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVER_VERSION = "2.2.1"

AGY_ROOT = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity"), ".gemini", "antigravity")
BRAIN_DIR = os.path.join(AGY_ROOT, "brain")
SUMMARIES_DB = os.path.join(AGY_ROOT, "conversation_summaries.db")
SUMMARIES_WAL = os.path.join(AGY_ROOT, "conversation_summaries.db-wal")
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")
PAYLOAD_HARD_LIMIT_BYTES = 30 * 1024 * 1024  # 30 MB 载荷红线

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
MODEL_SEL_RE = re.compile(r"changed setting `Model Selection` from .*? to ([^\.\n]+)")
BG_TASK_START_RE = re.compile(
    r"Tool is running as a background task with task id:\s*([0-9a-f]{8}-[0-9a-f\-]+/task-\d+)", re.I
)
BG_TASK_DESC_RE = re.compile(r"Task Description:\s*(.*)", re.S)
CREATED_LOCAL_RE = re.compile(r"Created At:\s*([0-9T:\-\+\.Z]+)")
COMPLETED_LOCAL_RE = re.compile(r"Completed At:\s*([0-9T:\-\+\.Z]+)")

# 日志时间戳与完成标志
REL_SEC_RE = re.compile(r"\+\s*(\d+(?:\.\d+)?)s\b")
FULL_DT_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2})\]")
HMS_BRACKET_RE = re.compile(r"\[(\d{2}:\d{2}:\d{2})(?:\s|\])")
TOTAL_RUNTIME_RE = re.compile(r"(?:Total runtime|elapsed)\s*=\s*(\d+(?:\.\d+)?)s", re.I)
COMPLETE_MARK_RE = re.compile(r"(?:\[COMPLETE\]|\bFINISHED:|\bQA PASSED:)", re.I)

# 严格进度与显式 ETA 模式（绝不误匹配 GPU=xx% 或 valid=xx%）
TQDM_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)%\|[^|\r\n]*\|\s*(\d+)\s*/\s*(\d+)\s*\[([0-9:]+)<([0-9:?]+)(?:,\s*([^\]]+))?\]"
)
EXPLICIT_ETA_UNIT_RE = re.compile(
    r"\bETA\s*[~:=≈]?\s*(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs)\b",
    re.I,
)
EXPLICIT_ETA_HMS_RE = re.compile(r"\bETA\s*[~:=≈]?\s*(\d{1,2}:\d{2}(?::\d{2})?)\b", re.I)
STEP_STAGE_RE = re.compile(r"\[\s*(?:Step|Stage)\s+(\d+)\s*/\s*(\d+)\s*\]", re.I)
LABELED_COUNTER_RE = re.compile(
    r"\b(?:epoch|step|stage|iter|batch|window|period|fold|chunk|round|进度|轮次|窗口|步骤)\s*[:=#\[]?\s*(\d+)\s*/\s*(\d+)\b",
    re.I,
)
PAREN_COUNTER_RE = re.compile(r"[\[\(]\s*(\d+)\s*/\s*(\d+)\s*[\]\)]")
EXPLICIT_PCT_RE = re.compile(r"(?:progress|进度|已完成|完成度)\s*[:=]\s*(\d{1,3}(?:\.\d+)?)\s*%", re.I)

# PowerShell foreach 多轮与外部重定向日志识别
PS_FOREACH_RE = re.compile(r"foreach\s*\(\s*\$(\w+)\s+in\s+([^\)]+)\)", re.I)
EXT_LOG_PATH_RE = re.compile(
    r'([A-Za-z]:\\[^"\'\r\n;|>*?]+?\.(?:stdout\.txt|stderr\.txt|log|out|txt))', re.I
)
PY_SCRIPT_PATH_RE = re.compile(r'([A-Za-z]:\\[^"\'\r\n;|>*?]+?\.py)\b', re.I)

_CACHE_LOCK = threading.Lock()
_CONV_SUMMARY_CACHE = {}  # conv_id -> (mtime_ns, size, fallback_title, step_count)
_RAW_STEPS_CACHE = {}     # conv_id -> (mtime_ns, size, raw_steps_list)
_DB_META_CACHE = {"sig": None, "map": {}}
_SIBLING_PROFILE_CACHE = {}  # filepath -> (mtime_ns, size, profile_dict)


def parse_iso_ts(ts_str):
    if not ts_str:
        return None
    s = ts_str.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def parse_hms_to_seconds(hms):
    if not hms or "?" in hms:
        return None
    parts = hms.strip().split(":")
    try:
        nums = [int(p) for p in parts]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
    except Exception:
        return None
    return None


def clean_unquoted_arg(val):
    if val is None:
        return ""
    s = str(val).strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            return json.loads(s)
        except Exception:
            return s[1:-1]
    return s


def read_tail_text(filepath, max_bytes=65536):
    try:
        size = os.path.getsize(filepath)
        with open(filepath, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read()
        return raw.decode("utf-8", errors="replace"), size
    except Exception:
        return "", 0


def normalize_terminal_lines(raw_text, max_lines=120):
    if not raw_text:
        return []
    out_lines = []
    for raw_line in raw_text.split("\n"):
        segments = [seg for seg in raw_line.split("\r") if seg.strip()]
        if segments:
            out_lines.append(segments[-1].rstrip())
        elif raw_line.strip():
            out_lines.append(raw_line.rstrip())
    return out_lines[-max_lines:]


def parse_line_rel_or_wall_sec(line, ref_start_ts):
    """提取日志行内的相对秒数 (+123.4s) 或挂钟时间戳"""
    m_rel = REL_SEC_RE.search(line)
    if m_rel:
        try:
            return float(m_rel.group(1))
        except Exception:
            pass
    m_dt = FULL_DT_RE.search(line)
    if m_dt and ref_start_ts:
        try:
            t = dt.datetime.fromisoformat(m_dt.group(1).replace(" ", "T")).timestamp()
            if t >= ref_start_ts - 5:
                return max(0.0, t - ref_start_ts)
        except Exception:
            pass
    m_hms = HMS_BRACKET_RE.search(line)
    if m_hms and ref_start_ts:
        try:
            ref_dt = dt.datetime.fromtimestamp(ref_start_ts)
            hh, mm, ss = [int(x) for x in m_hms.group(1).split(":")]
            cand = ref_dt.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            if cand.timestamp() < ref_start_ts - 60:
                cand += dt.timedelta(days=1)
            return max(0.0, cand.timestamp() - ref_start_ts)
        except Exception:
            pass
    return None


def parse_explicit_eta_sec(line):
    m = EXPLICIT_ETA_UNIT_RE.search(line)
    if m:
        val = float(m.group(1))
        u = m.group(2).lower()
        if u.startswith("h"):
            return val * 3600.0
        if u.startswith("m"):
            return val * 60.0
        return val
    m2 = EXPLICIT_ETA_HMS_RE.search(line)
    if m2:
        return parse_hms_to_seconds(m2.group(1))
    return None


def load_sibling_step_profile(log_dir, step_total, exclude_paths=()):
    """扫描同目录下已完成的日志，提取真实的 [Step k/N] 耗时分布曲线与总耗时"""
    if not log_dir or not os.path.isdir(log_dir):
        return None
    best_profile = None
    best_mtime = 0.0
    excl = {os.path.normcase(os.path.abspath(p)) for p in exclude_paths if p}

    for pat in ("*.stdout.txt", "*.log", "*.out"):
        for fp in glob.glob(os.path.join(log_dir, pat)):
            norm_fp = os.path.normcase(os.path.abspath(fp))
            if norm_fp in excl:
                continue
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if st.st_size < 120 or st.st_size > 8 * 1024 * 1024:
                continue

            ck = (st.st_mtime_ns, st.st_size)
            with _CACHE_LOCK:
                cached = _SIBLING_PROFILE_CACHE.get(norm_fp)
            if cached and cached[:2] == ck:
                prof = cached[2]
            else:
                prof = None
                txt, _ = read_tail_text(fp, max_bytes=131072)
                if COMPLETE_MARK_RE.search(txt):
                    lines = normalize_terminal_lines(txt, max_lines=300)
                    step_times = {}
                    total_rt = None
                    for ln in lines:
                        m_tr = TOTAL_RUNTIME_RE.search(ln)
                        if m_tr:
                            total_rt = float(m_tr.group(1))
                        ms = STEP_STAGE_RE.search(ln)
                        if ms:
                            sk, stot = int(ms.group(1)), int(ms.group(2))
                            rsec = parse_line_rel_or_wall_sec(ln, st.st_ctime)
                            if rsec is not None and (stot, sk) not in step_times:
                                step_times[(stot, sk)] = rsec
                    if total_rt and total_rt > 5.0 and step_times:
                        prof = {"total_sec": total_rt, "step_times": step_times, "ref_file": os.path.basename(fp)}
                with _CACHE_LOCK:
                    _SIBLING_PROFILE_CACHE[norm_fp] = (st.st_mtime_ns, st.st_size, prof)

            if prof and any(stot == step_total for (stot, _) in prof["step_times"]):
                if st.st_mtime > best_mtime:
                    best_mtime = st.st_mtime
                    best_profile = prof
    return best_profile


def analyze_single_run_log(lines, run_elapsed_sec, run_start_ts, log_mtime, log_dir="", exclude_paths=(), hist_script_sec=None):
    """对单次运行日志执行多级交叉验证进度与 ETA 推算"""
    now_ts = time.time()
    age_since_mtime = max(0.0, now_ts - log_mtime) if log_mtime else 0.0

    if not lines:
        if hist_script_sec and hist_script_sec > run_elapsed_sec:
            pct = min(95.0, run_elapsed_sec * 100.0 / hist_script_sec)
            return {
                "percent": round(pct, 1),
                "current": None,
                "total": None,
                "eta_sec": round(hist_script_sec - run_elapsed_sec, 1),
                "rate": "",
                "method": "按同类脚本历史实测耗时预估",
            }
        return None

    tail_joined = "\n".join(lines[-8:])
    if COMPLETE_MARK_RE.search(tail_joined):
        m_rt = TOTAL_RUNTIME_RE.search(tail_joined)
        return {
            "percent": 100.0,
            "current": None,
            "total": None,
            "eta_sec": 0.0,
            "measured_total_sec": float(m_rt.group(1)) if m_rt else run_elapsed_sec,
            "rate": "已完成",
            "method": "日志已输出完成标志",
        }

    # 1) 优先检查标准 tqdm 进度条
    for ln in reversed(lines[-25:]):
        m = TQDM_RE.search(ln)
        if m:
            cur, tot = int(m.group(2)), int(m.group(3))
            pct = (cur * 100.0 / tot) if tot > 0 else float(m.group(1))
            raw_eta = parse_hms_to_seconds(m.group(5))
            rate_str = (m.group(6) or "").strip()
            if raw_eta is not None:
                eta = max(0.0, raw_eta - age_since_mtime)
            elif pct > 0.3 and run_elapsed_sec > 1:
                eta = max(0.0, run_elapsed_sec * (100.0 - pct) / pct)
            else:
                eta = None
            return {
                "percent": round(min(99.9, pct), 1),
                "current": cur,
                "total": tot,
                "eta_sec": round(eta, 1) if eta is not None else None,
                "rate": rate_str,
                "method": "tqdm 实时进度条",
            }

    # 2) 检查 [Step k/N] 阶段型日志 + 同目录已完成日志经验曲线校准
    step_events = []
    latest_explicit_eta = None
    latest_explicit_eta_rsec = None
    latest_any_rsec = None

    for ln in lines:
        rsec = parse_line_rel_or_wall_sec(ln, run_start_ts)
        if rsec is not None:
            latest_any_rsec = rsec
        e_sec = parse_explicit_eta_sec(ln)
        if e_sec is not None:
            latest_explicit_eta = e_sec
            latest_explicit_eta_rsec = rsec
        ms = STEP_STAGE_RE.search(ln)
        if ms:
            step_events.append((int(ms.group(1)), int(ms.group(2)), rsec, ln))

    if step_events:
        cur_step, tot_steps, step_rsec, _ = step_events[-1]
        eff_elapsed = max(run_elapsed_sec, latest_any_rsec or 0.0)
        prof = load_sibling_step_profile(log_dir, tot_steps, exclude_paths=exclude_paths)
        if prof and (tot_steps, cur_step) in prof["step_times"]:
            ref_total = prof["total_sec"]
            ref_t_at_step = prof["step_times"][(tot_steps, cur_step)]
            # 若已有非零步骤耗时，动态计算当前机器/数据规模相对参考日志的倍率因子
            scale = 1.0
            if step_rsec and ref_t_at_step >= 12.0:
                scale = max(0.7, min(2.8, step_rsec / ref_t_at_step))
            # 如果在当前步骤停留的时间已经超过了参考下一步时间，平滑外推倍率
            next_ref_t = prof["step_times"].get((tot_steps, cur_step + 1), ref_total)
            if eff_elapsed > next_ref_t * scale and next_ref_t > 5.0:
                scale = max(scale, min(3.0, eff_elapsed / next_ref_t * 1.08))

            est_total = max(eff_elapsed + 8.0, ref_total * scale)
            eta = max(0.0, est_total - eff_elapsed)
            pct = min(99.0, eff_elapsed * 100.0 / est_total)
            return {
                "percent": round(pct, 1),
                "current": cur_step,
                "total": tot_steps,
                "eta_sec": round(eta, 1),
                "expected_total_sec": round(est_total, 1),
                "rate": f"阶段 {cur_step}/{tot_steps}",
                "method": f"同目录实测曲线校准 ({prof['ref_file']})",
            }

        # 无同目录已完成日志时，综合显式 ETA（扣减自打印时刻起的流逝秒数）与同类脚本历史耗时
        eta_candidates = []
        if latest_explicit_eta is not None:
            passed_since_eta = (
                max(0.0, eff_elapsed - latest_explicit_eta_rsec)
                if latest_explicit_eta_rsec is not None
                else age_since_mtime
            )
            rem_exp = latest_explicit_eta - passed_since_eta
            if rem_exp > 5.0:
                eta_candidates.append(rem_exp)
        if hist_script_sec and hist_script_sec > eff_elapsed:
            eta_candidates.append(hist_script_sec - eff_elapsed)

        eta = max(eta_candidates) if eta_candidates else None
        pct = (
            min(98.0, eff_elapsed * 100.0 / (eff_elapsed + eta))
            if (eta is not None and eff_elapsed + eta > 0)
            else round((cur_step * 100.0 / max(1, tot_steps)), 1)
        )
        return {
            "percent": round(pct, 1),
            "current": cur_step,
            "total": tot_steps,
            "eta_sec": round(eta, 1) if eta is not None else None,
            "rate": f"阶段 {cur_step}/{tot_steps}",
            "method": "日志阶段 + 历史耗时联合校准" if hist_script_sec else "脚本内置阶段估算",
        }

    # 3) 均匀步数计数器 (如 Epoch 15/60, (252/253), [120/251]) —— 采用时间戳增量速率消除断点续传误差
    counter_pts = []
    for ln in lines:
        m = LABELED_COUNTER_RE.search(ln) or PAREN_COUNTER_RE.search(ln)
        if m:
            c_val, t_val = int(m.group(1)), int(m.group(2))
            if 1 < t_val <= 1000000 and 0 < c_val <= t_val:
                # 排除类似日期 (11/11) 的小数字偶然匹配，除非带明确关键字或 tot >= 5
                if t_val >= 5 or LABELED_COUNTER_RE.search(ln):
                    rsec = parse_line_rel_or_wall_sec(ln, run_start_ts)
                    counter_pts.append((c_val, t_val, rsec, ln))

    if counter_pts:
        cur_c, tot_c, last_rsec, _ = counter_pts[-1]
        same_tot = [p for p in counter_pts if p[1] == tot_c]
        pct = cur_c * 100.0 / tot_c
        eta = None
        rate_str = ""

        # 寻找最早且有时间戳的不同进度点计算真实边际速度 Δc / Δt
        timed = [p for p in same_tot if p[2] is not None]
        if len(timed) >= 2 and timed[-1][0] > timed[0][0] and (timed[-1][2] - timed[0][2]) >= 2.0:
            dc = timed[-1][0] - timed[0][0]
            dt_s = timed[-1][2] - timed[0][2]
            sec_per_item = dt_s / dc
            since_last = max(0.0, run_elapsed_sec - timed[-1][2])
            eta = max(0.0, (tot_c - cur_c) * sec_per_item - since_last)
            rate_str = f"{1.0 / sec_per_item:.2f} 步/秒" if sec_per_item < 1.0 else f"{sec_per_item:.1f} 秒/步"
            method = "实测边际步速推算 (Δ步/Δ时)"
        elif cur_c > 0 and run_elapsed_sec > 2.0:
            sec_per_item = run_elapsed_sec / cur_c
            eta = max(0.0, (tot_c - cur_c) * sec_per_item)
            rate_str = f"{1.0 / sec_per_item:.2f} 步/秒" if sec_per_item < 1.0 else f"{sec_per_item:.1f} 秒/步"
            method = "平均步速线性推算"
        else:
            method = "步数计数识别"

        return {
            "percent": round(min(100.0, pct), 1),
            "current": cur_c,
            "total": tot_c,
            "eta_sec": round(eta, 1) if eta is not None else None,
            "rate": rate_str,
            "method": method,
        }

    # 4) 仅带明确“进度/progress”前缀的百分比或显式 ETA
    if latest_explicit_eta is not None:
        eff_el = max(run_elapsed_sec, latest_any_rsec or 0.0)
        passed = (eff_el - latest_explicit_eta_rsec) if latest_explicit_eta_rsec is not None else age_since_mtime
        rem = max(0.0, latest_explicit_eta - max(0.0, passed))
        pct = (eff_el * 100.0 / (eff_el + rem)) if (eff_el + rem > 0) else None
        return {
            "percent": round(pct, 1) if pct is not None else None,
            "current": None,
            "total": None,
            "eta_sec": round(rem, 1),
            "rate": "",
            "method": "脚本显式 ETA 实时倒计",
        }

    for ln in reversed(lines[-20:]):
        mp = EXPLICIT_PCT_RE.search(ln)
        if mp:
            pct = float(mp.group(1))
            eta = (
                max(0.0, run_elapsed_sec * (100.0 - pct) / pct)
                if (0.5 < pct < 100.0 and run_elapsed_sec > 2.0)
                else None
            )
            return {
                "percent": round(pct, 1),
                "current": None,
                "total": None,
                "eta_sec": round(eta, 1) if eta is not None else None,
                "rate": "",
                "method": "显式进度百分比推算",
            }

    # 5) 若无任何进度行，但有同脚本目录历史实测耗时，给出历史参考
    if hist_script_sec and hist_script_sec > run_elapsed_sec:
        pct = min(95.0, run_elapsed_sec * 100.0 / hist_script_sec)
        return {
            "percent": round(pct, 1),
            "current": None,
            "total": None,
            "eta_sec": round(hist_script_sec - run_elapsed_sec, 1),
            "rate": "",
            "method": f"按同类脚本历史实测耗时 ({int(hist_script_sec)}s) 预估",
        }

    return None


def resolve_external_logs_and_progress(command, start_ts, elapsed_sec, default_lines, default_bytes, default_mtime, hist_script_sec):
    """自动发现命令重定向的外部日志文件及 PowerShell foreach 多轮循环，合成总进度与总 ETA"""
    now_ts = time.time()
    m_loop = PS_FOREACH_RE.search(command or "")
    m_paths = EXT_LOG_PATH_RE.findall(command or "")

    # 情况 A：PowerShell foreach ($var in v1,v2,v3) 多轮种子循环
    if m_loop and m_paths:
        var_name = m_loop.group(1)
        raw_items = [x.strip().strip("\"'") for x in m_loop.group(2).split(",") if x.strip()]
        tpl_path = None
        for p in m_paths:
            if f"${var_name}" in p or f"${{{var_name}}}" in p:
                tpl_path = p
                break
        if tpl_path and len(raw_items) >= 2:
            n_loop = len(raw_items)
            expanded = [
                tpl_path.replace(f"${{{var_name}}}", it).replace(f"${var_name}", it)
                for it in raw_items
            ]
            done_durs = []
            last_done_mtime = None
            active_idx = None
            active_fp = None
            active_lines = []
            active_bytes = 0
            active_mtime = default_mtime

            for i, fp in enumerate(expanded):
                if not os.path.isfile(fp):
                    continue
                try:
                    fst = os.stat(fp)
                except OSError:
                    continue
                if fst.st_mtime < start_ts - 15:
                    continue
                txt, fsz = read_tail_text(fp)
                lns = normalize_terminal_lines(txt, max_lines=120)
                tail_s = "\n".join(lns[-8:])
                # 判断该轮是否已跑完（存在 COMPLETE 标志，或下一轮文件已经创建）
                next_started = (
                    (i + 1 < n_loop)
                    and os.path.isfile(expanded[i + 1])
                    and os.path.getmtime(expanded[i + 1]) >= fst.st_mtime - 2
                )
                if COMPLETE_MARK_RE.search(tail_s) or next_started:
                    m_rt = TOTAL_RUNTIME_RE.search(tail_s)
                    dur_i = float(m_rt.group(1)) if m_rt else max(1.0, fst.st_mtime - max(start_ts, fst.st_ctime))
                    done_durs.append(dur_i)
                    last_done_mtime = fst.st_mtime
                    # 若所有轮次都刚完成，保留最后一轮日志以供展示
                    active_fp, active_lines, active_bytes, active_mtime = fp, lns, fsz, fst.st_mtime
                else:
                    active_idx = i
                    active_fp, active_lines, active_bytes, active_mtime = fp, lns, fsz, fst.st_mtime
                    break

            done_cnt = len(done_durs)
            if done_cnt >= n_loop:
                return (
                    {
                        "percent": 100.0,
                        "current": n_loop,
                        "total": n_loop,
                        "eta_sec": 0.0,
                        "rate": f"全部 {n_loop} 轮已完成",
                        "method": "多轮循环收尾中",
                    },
                    active_lines[-18:],
                    active_bytes,
                    active_mtime,
                    os.path.basename(active_fp) if active_fp else "",
                )

            cur_idx = active_idx if active_idx is not None else done_cnt
            cur_item_label = raw_items[cur_idx]
            cur_seed_start = last_done_mtime if last_done_mtime else ((start_ts + sum(done_durs)) if done_durs else start_ts)
            cur_seed_elapsed = max(0.0, now_ts - cur_seed_start)
            ref_seed_sec = (sum(done_durs) / len(done_durs)) if done_durs else hist_script_sec

            sub_prog = analyze_single_run_log(
                active_lines,
                cur_seed_elapsed,
                cur_seed_start,
                active_mtime,
                log_dir=os.path.dirname(active_fp) if active_fp else "",
                exclude_paths=expanded[cur_idx:],  # 允许引用本轮之前刚跑完的 s42 作为 s43/s44 的精确经验曲线！
                hist_script_sec=ref_seed_sec,
            )

            cur_pct = (sub_prog["percent"] if (sub_prog and sub_prog.get("percent") is not None) else 0.0)
            cur_eta = sub_prog.get("eta_sec") if sub_prog else None
            per_seed_est = (
                (sum(done_durs) / len(done_durs))
                if done_durs
                else (
                    sub_prog.get("expected_total_sec")
                    or ((cur_seed_elapsed + cur_eta) if cur_eta is not None else hist_script_sec)
                )
            )

            rem_seeds = max(0, n_loop - done_cnt - 1)
            total_eta = (
                (cur_eta + rem_seeds * per_seed_est)
                if (cur_eta is not None and per_seed_est is not None)
                else None
            )
            total_pct = ((done_cnt + cur_pct / 100.0) / n_loop) * 100.0
            sub_rate = f"第 {cur_idx + 1}/{n_loop} 轮 (seed={cur_item_label})"
            if sub_prog and sub_prog.get("rate"):
                sub_rate += f" · {sub_prog['rate']}"
            method_note = (
                f"多轮循环 ({done_cnt}/{n_loop} 已完) + {sub_prog['method']}"
                if sub_prog
                else f"多轮循环 ({cur_idx + 1}/{n_loop})"
            )
            header_line = f"[外部实时日志: {active_fp}]" if active_fp else ""
            out_tail = ([header_line] + active_lines[-16:]) if header_line else active_lines[-16:]
            return (
                {
                    "percent": round(min(99.5, total_pct), 1),
                    "current": cur_idx + 1,
                    "total": n_loop,
                    "eta_sec": round(total_eta, 1) if total_eta is not None else None,
                    "rate": sub_rate,
                    "method": method_note,
                },
                out_tail,
                active_bytes,
                active_mtime,
                os.path.basename(active_fp) if active_fp else "",
            )

    # 情况 B：单个外部重定向日志文件
    ext_fp = None
    for p in m_paths:
        if "$" not in p and os.path.isfile(p):
            try:
                if os.path.getmtime(p) >= start_ts - 15:
                    ext_fp = p
                    break
            except OSError:
                pass

    if ext_fp and (default_bytes < 64 or os.path.getmtime(ext_fp) >= (default_mtime or 0)):
        txt, fsz = read_tail_text(ext_fp)
        lns = normalize_terminal_lines(txt, max_lines=120)
        fmtime = os.path.getmtime(ext_fp)
        prog = analyze_single_run_log(
            lns,
            elapsed_sec,
            start_ts,
            fmtime,
            log_dir=os.path.dirname(ext_fp),
            exclude_paths=(ext_fp,),
            hist_script_sec=hist_script_sec,
        )
        return prog, [f"[外部实时日志: {ext_fp}]"] + lns[-16:], fsz, fmtime, os.path.basename(ext_fp)

    # 情况 C：默认直接解析 task-xxx.log（若命令中能识别出 .py 脚本目录，同样支持同目录经验曲线校准）
    py_m = PY_SCRIPT_PATH_RE.search(command or "")
    script_dir = os.path.dirname(py_m.group(1)) if py_m else ""
    prog = analyze_single_run_log(
        default_lines,
        elapsed_sec,
        start_ts,
        default_mtime,
        log_dir=script_dir,
        exclude_paths=(),
        hist_script_sec=hist_script_sec,
    )
    return prog, default_lines[-16:], default_bytes, default_mtime, ""


def extract_script_family_key(cmd):
    """提取 Python 脚本所在目录作为同类长任务的匹配键（排除 python -c 等临时单行命令）"""
    if not cmd:
        return ""
    m = PY_SCRIPT_PATH_RE.search(cmd)
    if not m:
        return ""
    sp = os.path.normcase(m.group(1))
    # 排除监控服务自身
    if "ag_monitor" in sp:
        return ""
    return os.path.dirname(sp)


def load_official_db_summaries():
    """只读读取 Antigravity 官方 conversation_summaries.db，同步侧边栏完全一致的会话名称与层级"""
    if not os.path.isfile(SUMMARIES_DB):
        return {}
    try:
        db_st = os.stat(SUMMARIES_DB)
        wal_ns = os.stat(SUMMARIES_WAL).st_mtime_ns if os.path.isfile(SUMMARIES_WAL) else 0
        sig = (db_st.st_mtime_ns, db_st.st_size, wal_ns)
    except OSError:
        return {}

    with _CACHE_LOCK:
        if _DB_META_CACHE["sig"] == sig and _DB_META_CACHE["map"]:
            return _DB_META_CACHE["map"]

    uri = "file:///" + SUMMARIES_DB.replace("\\", "/").lstrip("/") + "?mode=ro"
    meta_map = {}
    try:
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT conversation_id, title, step_count, status, nesting_depth,
                   parent_conversation_id, workspace_uris, last_modified_time
            FROM conversation_summaries
            WHERE killed = 0
            """
        ).fetchall()
        con.close()
        for cid, title, sc, status, depth, parent_id, ws_raw, lmt in rows:
            ws_name = ""
            if ws_raw:
                try:
                    uris = json.loads(ws_raw)
                    if uris:
                        u = urllib.parse.unquote(str(uris[0])).rstrip("/\\")
                        ws_name = u.split("/")[-1].split("\\")[-1]
                except Exception:
                    pass
            lmt_ts = None
            if lmt:
                s = str(lmt).strip().replace(" ", "T")
                s = re.sub(r"(\.\d{6})\d+", r"\1", s)
                lmt_ts = parse_iso_ts(s)
            meta_map[cid] = {
                "title": (title or "").strip(),
                "step_count": int(sc or 0),
                "status": status or "",
                "is_running": status == "CASCADE_RUN_STATUS_RUNNING",
                "nesting_depth": int(depth or 0),
                "parent_id": (parent_id or "").strip(),
                "workspace": ws_name,
                "db_modified_ts": lmt_ts,
            }
        with _CACHE_LOCK:
            _DB_META_CACHE["sig"] = sig
            _DB_META_CACHE["map"] = meta_map
    except Exception:
        with _CACHE_LOCK:
            return dict(_DB_META_CACHE["map"])
    return meta_map


def get_conversation_summary(conv_id, db_map=None):
    if db_map is None:
        db_map = load_official_db_summaries()
    db_info = db_map.get(conv_id)
    if db_info and (db_info["nesting_depth"] > 0 or db_info["parent_id"]):
        return None

    conv_dir = os.path.join(BRAIN_DIR, conv_id)
    tr_path = os.path.join(conv_dir, ".system_generated", "logs", "transcript.jsonl")
    if not os.path.isfile(tr_path):
        return None
    try:
        st = os.stat(tr_path)
    except OSError:
        return None

    tasks_dir = os.path.join(conv_dir, ".system_generated", "tasks")
    latest_mtime = st.st_mtime
    if os.path.isdir(tasks_dir):
        for fn in os.listdir(tasks_dir):
            fp = os.path.join(tasks_dir, fn)
            try:
                latest_mtime = max(latest_mtime, os.path.getmtime(fp))
            except OSError:
                pass
    if db_info and db_info.get("db_modified_ts"):
        latest_mtime = max(latest_mtime, db_info["db_modified_ts"])

    official_title = (db_info["title"] if db_info else "").strip()
    cache_key = (st.st_mtime_ns, st.st_size)
    fallback_title = ""
    step_count = db_info["step_count"] if (db_info and db_info["step_count"] > 0) else 0

    with _CACHE_LOCK:
        cached = _CONV_SUMMARY_CACHE.get(conv_id)
    if cached and cached[:2] == cache_key:
        fallback_title, cached_sc = cached[2], cached[3]
        if not step_count:
            step_count = cached_sc
    elif not official_title or not step_count:
        sc = 0
        try:
            with open(tr_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    sc += 1
                    if not fallback_title and '"USER_INPUT"' in line:
                        try:
                            obj = json.loads(line)
                            c = obj.get("content") or ""
                            m = USER_REQ_RE.search(c)
                            txt = (m.group(1) if m else c).strip()
                            txt_first = txt.splitlines()[0].strip() if txt else ""
                            if txt_first:
                                fallback_title = txt_first[:48]
                        except Exception:
                            pass
        except Exception:
            pass
        if not step_count:
            step_count = sc
        with _CACHE_LOCK:
            _CONV_SUMMARY_CACHE[conv_id] = (st.st_mtime_ns, st.st_size, fallback_title, sc)

    final_title = official_title or fallback_title
    if not final_title:
        return None

    return {
        "id": conv_id,
        "short_id": conv_id[:8],
        "title": final_title,
        "workspace": db_info["workspace"] if db_info else "",
        "is_running": bool(db_info["is_running"]) if db_info else False,
        "step_count": step_count,
        "transcript_bytes": st.st_size,
        "updated_ts": st.st_mtime,
        "latest_activity_ts": latest_mtime,
    }


def list_conversations(limit=35):
    if not os.path.isdir(BRAIN_DIR):
        return []
    db_map = load_official_db_summaries()
    candidates = []
    for name in os.listdir(BRAIN_DIR):
        if not UUID_RE.match(name):
            continue
        db_info = db_map.get(name)
        if db_info and (db_info["nesting_depth"] > 0 or db_info["parent_id"]):
            continue
        tr_path = os.path.join(BRAIN_DIR, name, ".system_generated", "logs", "transcript.jsonl")
        if os.path.isfile(tr_path):
            try:
                ts = os.path.getmtime(tr_path)
                if db_info and db_info.get("db_modified_ts"):
                    ts = max(ts, db_info["db_modified_ts"])
                candidates.append((ts, name))
            except OSError:
                pass
    candidates.sort(reverse=True)
    results = []
    for _, cid in candidates[: limit + 15]:
        s = get_conversation_summary(cid, db_map=db_map)
        if s:
            results.append(s)
        if len(results) >= limit:
            break
    results.sort(key=lambda x: x["latest_activity_ts"], reverse=True)
    return results


def parse_subagents(conv_dir, now_ts, db_map=None):
    sub_dir = os.path.join(conv_dir, ".system_generated", "subagents")
    if not os.path.isdir(sub_dir):
        return []
    if db_map is None:
        db_map = load_official_db_summaries()
    subs = []
    for fn in sorted(os.listdir(sub_dir)):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(sub_dir, fn)
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                meta = json.load(f)
            sub_id = meta.get("conversationId", fn[:-5])
            desc = meta.get("subagentDescriptor") or {}
            role = desc.get("role") or desc.get("typeName") or "子智能体"
            type_name = desc.get("typeName") or "subagent"
            state = meta.get("state") or ""
            sub_db = db_map.get(sub_id)

            sub_tr = os.path.join(BRAIN_DIR, sub_id, ".system_generated", "logs", "transcript.jsonl")
            sub_steps = 0
            sub_tokens = 0
            sub_ctx = 0
            sub_start_ts = None
            sub_last_ts = None
            sub_active_sec = 0.0
            prev_ts = None
            prev_was_idle_reply = False
            last_step_idle_reply = False
            sub_last_action = "初始化中"
            if os.path.isfile(sub_tr):
                with open(sub_tr, "r", encoding="utf-8", errors="replace") as sf:
                    for sline in sf:
                        sline = sline.strip()
                        if not sline:
                            continue
                        sub_steps += 1
                        try:
                            sobj = json.loads(sline)
                            cts = parse_iso_ts(sobj.get("created_at"))
                            if cts:
                                if sub_start_ts is None:
                                    sub_start_ts = cts
                                if prev_ts is not None and cts >= prev_ts:
                                    dt_gap = cts - prev_ts
                                    # 排除子智能体已完成一轮待命、数分钟后被再次唤醒之间的空闲等待时间
                                    if not (prev_was_idle_reply and dt_gap > 60.0):
                                        sub_active_sec += dt_gap
                                prev_ts = cts
                                sub_last_ts = cts
                            in_t = int(sobj.get("input_tokens") or 0)
                            ca_t = int(sobj.get("cache_read_tokens") or 0)
                            ou_t = int(sobj.get("output_tokens") or 0)
                            if in_t + ca_t > 0:
                                sub_ctx = in_t + ca_t
                            sub_tokens += in_t + ou_t
                            tcs = sobj.get("tool_calls") or []
                            if tcs:
                                sub_last_action = f"调用 {tcs[-1].get('name', 'tool')}"
                                prev_was_idle_reply = False
                                last_step_idle_reply = False
                            elif sobj.get("type") == "PLANNER_RESPONSE":
                                sub_last_action = "已返回结果"
                                prev_was_idle_reply = True
                                last_step_idle_reply = True
                            else:
                                prev_was_idle_reply = False
                                last_step_idle_reply = False
                        except Exception:
                            pass

            if sub_db is not None:
                is_alive = ("ALIVE" in state) and bool(sub_db.get("is_running"))
                # 若数据库记录了完成写入时间（如最后一步纯文本回复耗时），补齐最后一步推理秒数
                db_lmt = sub_db.get("db_modified_ts")
                if not is_alive and db_lmt and sub_last_ts and 0 < (db_lmt - sub_last_ts) < 120:
                    sub_active_sec += db_lmt - sub_last_ts
            else:
                is_alive = (
                    ("ALIVE" in state)
                    and not last_step_idle_reply
                    and (sub_last_ts is None or (now_ts - sub_last_ts) < 300)
                )

            elapsed = (sub_active_sec + max(0.0, now_ts - sub_last_ts)) if (is_alive and sub_last_ts) else sub_active_sec
            if "KILLED" in state:
                state_cn = "已终止"
            elif is_alive:
                state_cn = "运行中"
            elif "ALIVE" in state:
                state_cn = "已完成"
            else:
                state_cn = state

            subs.append(
                {
                    "id": sub_id,
                    "short_id": sub_id[:8],
                    "role": role,
                    "type_name": type_name,
                    "state": state_cn,
                    "is_running": is_alive,
                    "steps": sub_steps,
                    "context_tokens": sub_ctx,
                    "total_tokens": sub_tokens,
                    "elapsed_sec": round(max(0.0, elapsed), 1),
                    "last_action": sub_last_action,
                }
            )
        except Exception:
            pass
    return subs


def get_conversation_detail(conv_id):
    now_ts = time.time()
    conv_dir = os.path.join(BRAIN_DIR, conv_id)
    sg_dir = os.path.join(conv_dir, ".system_generated")
    tr_path = os.path.join(sg_dir, "logs", "transcript.jsonl")
    tr_full_path = os.path.join(sg_dir, "logs", "transcript_full.jsonl")
    tasks_dir = os.path.join(sg_dir, "tasks")

    if not os.path.isfile(tr_path):
        return {"error": f"未找到会话日志: {tr_path}"}

    st = os.stat(tr_path)
    full_bytes = os.path.getsize(tr_full_path) if os.path.isfile(tr_full_path) else st.st_size

    step_key = (st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        cached_steps = _RAW_STEPS_CACHE.get(conv_id)
    if cached_steps and cached_steps[:2] == step_key:
        raw_steps = cached_steps[2]
    else:
        raw_steps = []
        with open(tr_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw_steps.append(json.loads(line))
                except Exception:
                    pass
        with _CACHE_LOCK:
            _RAW_STEPS_CACHE[conv_id] = (st.st_mtime_ns, st.st_size, raw_steps)

    model_name = "Gemini (默认)"
    title = ""
    total_input_tokens = 0
    total_cache_tokens = 0
    total_output_tokens = 0
    latest_context_tokens = 0
    latest_cache_ratio = 0.0
    latest_input_tokens = 0
    latest_output_tokens = 0

    turn_count = 0
    compact_count = 0
    last_ckpt_idx = 0
    seen_media_paths = set()
    media_raw_bytes = 0
    first_ts = None
    last_ts = None
    last_user_turn_ts = None

    planner_metrics = []
    timeline_steps = []
    bg_tasks = {}  # task_short_id -> dict

    for idx, step in enumerate(raw_steps):
        s_idx = step.get("step_index", idx)
        s_type = step.get("type", "")
        s_status = step.get("status", "")
        cts = parse_iso_ts(step.get("created_at"))
        if cts:
            if first_ts is None:
                first_ts = cts
            last_ts = cts

        if s_type == "CHECKPOINT":
            compact_count += 1
            last_ckpt_idx = idx

        for m_item in step.get("media") or []:
            m_uri = (m_item.get("uri") or "").strip()
            if m_uri.startswith("file:///"):
                m_uri = urllib.parse.unquote(m_uri[8:])
            if m_uri and m_uri not in seen_media_paths and os.path.isfile(m_uri):
                seen_media_paths.add(m_uri)
                try:
                    media_raw_bytes += os.path.getsize(m_uri)
                except OSError:
                    pass

        content = step.get("content") or ""
        if s_type == "USER_INPUT" and "<USER_SETTINGS_CHANGE>" in content:
            mm = MODEL_SEL_RE.search(content)
            if mm and len(mm.group(1).strip()) < 64:
                model_name = mm.group(1).strip()

        next_ts = None
        if idx + 1 < len(raw_steps):
            next_ts = parse_iso_ts(raw_steps[idx + 1].get("created_at"))

        if s_type == "USER_INPUT":
            turn_count += 1
            if cts:
                last_user_turn_ts = cts
            # 任何 TimerCondition="any" 的定时器在收到新用户消息后已结束
            for tinfo in bg_tasks.values():
                if tinfo["status"] == "RUNNING" and tinfo.get("is_timer") and tinfo.get("timer_cond") == "any":
                    tinfo["status"] = "COMPLETED"
                    tinfo["end_ts"] = cts or now_ts

            m = USER_REQ_RE.search(content)
            prompt_txt = (m.group(1) if m else content).strip()
            first_line = prompt_txt.splitlines()[0].strip() if prompt_txt else "用户输入"
            if not title and first_line:
                title = first_line[:60]
            timeline_steps.append(
                {
                    "step_index": s_idx,
                    "kind": "user",
                    "label": "用户提问",
                    "summary": first_line[:90],
                    "status": s_status,
                    "created_ts": cts,
                    "duration_sec": round(max(0.0, next_ts - cts), 1) if (cts and next_ts) else None,
                    "context_tokens": None,
                    "output_tokens": None,
                    "tok_per_sec": None,
                }
            )

        elif s_type == "PLANNER_RESPONSE":
            in_tok = int(step.get("input_tokens") or 0)
            ca_tok = int(step.get("cache_read_tokens") or 0)
            out_tok = int(step.get("output_tokens") or 0)
            ctx_tok = in_tok + ca_tok

            total_input_tokens += in_tok
            total_cache_tokens += ca_tok
            total_output_tokens += out_tok
            if ctx_tok > 0:
                latest_context_tokens = ctx_tok
                latest_cache_ratio = (ca_tok * 100.0 / ctx_tok) if ctx_tok > 0 else 0.0
                latest_input_tokens = in_tok
                latest_output_tokens = out_tok

            dur = None
            if idx + 1 < len(raw_steps):
                nxt = raw_steps[idx + 1]
                nxt_ts = parse_iso_ts(nxt.get("created_at"))
                if nxt.get("type") == "GENERIC":
                    m_ca = CREATED_LOCAL_RE.search(nxt.get("content") or "")
                    tool_start_ts = parse_iso_ts(m_ca.group(1)) if m_ca else nxt_ts
                    if cts and tool_start_ts and tool_start_ts >= cts:
                        dur = tool_start_ts - cts
                elif nxt.get("type") not in ("USER_INPUT", "SYSTEM_MESSAGE") and cts and nxt_ts and nxt_ts >= cts:
                    dur = nxt_ts - cts

            tok_s = round(out_tok / dur, 1) if (dur and dur >= 0.4 and out_tok > 0) else None

            tcs = step.get("tool_calls") or []
            if tcs:
                names = []
                for tc in tcs:
                    tname = tc.get("name", "tool")
                    targs = tc.get("args") or {}
                    tsum = clean_unquoted_arg(targs.get("toolSummary", ""))
                    names.append(f"{tname} ({tsum})" if tsum else tname)
                    if tname == "manage_task" and clean_unquoted_arg(targs.get("Action")) == "kill":
                        ktid = clean_unquoted_arg(targs.get("TaskId", "")).split("/")[-1]
                        if ktid in bg_tasks and bg_tasks[ktid]["status"] == "RUNNING":
                            bg_tasks[ktid]["status"] = "STOPPED"
                            bg_tasks[ktid]["end_ts"] = cts or now_ts
                summary = " → " + "；".join(names)
                label = "思考与工具调用"
            else:
                ans = (step.get("content") or "").strip()
                first_ans = ans.splitlines()[0].strip() if ans else "输出回复"
                summary = first_ans[:90]
                label = "模型回复"

            planner_metrics.append(
                {
                    "step_index": s_idx,
                    "created_ts": cts,
                    "context_tokens": ctx_tok,
                    "input_tokens": in_tok,
                    "cache_tokens": ca_tok,
                    "output_tokens": out_tok,
                    "duration_sec": round(dur, 2) if dur is not None else None,
                    "tok_per_sec": tok_s,
                }
            )
            timeline_steps.append(
                {
                    "step_index": s_idx,
                    "kind": "model",
                    "label": label,
                    "summary": summary,
                    "status": s_status,
                    "created_ts": cts,
                    "duration_sec": round(dur, 1) if dur is not None else None,
                    "context_tokens": ctx_tok,
                    "output_tokens": out_tok,
                    "tok_per_sec": tok_s,
                }
            )

        elif s_type == "GENERIC":
            head_chunk = content[:240]
            is_bg_header = head_chunk.lstrip().startswith("Created At:") and (
                "\nTool is running as a background task with task id:" in head_chunk
            )
            m_bg = BG_TASK_START_RE.search(head_chunk) if is_bg_header else None
            if m_bg and m_bg.group(1).strip().lower().startswith(conv_id.lower() + "/"):
                full_task_id = m_bg.group(1).strip()
                short_tid = full_task_id.split("/")[-1]
                if short_tid in bg_tasks:
                    continue
                m_desc = BG_TASK_DESC_RE.search(content)
                cmd_desc = (m_desc.group(1).strip().splitlines()[0] if m_desc else "").strip()

                tool_summary = ""
                full_cmd = cmd_desc
                is_timer = cmd_desc.startswith("Timer:")
                timer_dur = None
                timer_cond = ""
                is_daemon_flag = False

                for back_i in range(idx - 1, max(-1, idx - 5), -1):
                    prev_s = raw_steps[back_i]
                    if prev_s.get("type") == "PLANNER_RESPONSE":
                        for tc in prev_s.get("tool_calls") or []:
                            if tc.get("name") == "schedule":
                                args = tc.get("args") or {}
                                is_timer = True
                                tool_summary = "[定时器] " + clean_unquoted_arg(args.get("Prompt") or short_tid)
                                full_cmd = cmd_desc
                                d_raw = clean_unquoted_arg(args.get("DurationSeconds") or "")
                                timer_dur = float(d_raw) if d_raw.replace(".", "", 1).isdigit() else None
                                timer_cond = clean_unquoted_arg(args.get("TimerCondition") or "")
                            elif tc.get("name") == "run_command":
                                args = tc.get("args") or {}
                                tool_summary = clean_unquoted_arg(args.get("toolSummary") or "")
                                full_cmd = clean_unquoted_arg(args.get("CommandLine") or cmd_desc)
                                is_daemon_flag = str(clean_unquoted_arg(args.get("IsDaemon") or "")).lower() == "true"
                        break

                m_ca = CREATED_LOCAL_RE.search(content)
                start_ts = parse_iso_ts(m_ca.group(1)) if m_ca else cts
                bg_tasks[short_tid] = {
                    "task_id": short_tid,
                    "full_task_id": full_task_id,
                    "title": tool_summary or short_tid,
                    "command": full_cmd[:360],
                    "start_ts": start_ts or cts or now_ts,
                    "end_ts": None,
                    "status": "RUNNING",
                    "is_timer": is_timer,
                    "timer_dur": timer_dur,
                    "timer_cond": timer_cond,
                    "is_daemon": is_daemon_flag,
                    "step_index": s_idx,
                }
                timeline_steps.append(
                    {
                        "step_index": s_idx,
                        "kind": "bg_start",
                        "label": f"启动后台 {short_tid}",
                        "summary": f"{tool_summary or short_tid}: {full_cmd[:70]}",
                        "status": "RUNNING",
                        "created_ts": start_ts or cts,
                        "duration_sec": None,
                        "context_tokens": None,
                        "output_tokens": None,
                        "tok_per_sec": None,
                    }
                )
            else:
                m_ca = CREATED_LOCAL_RE.search(content)
                m_co = COMPLETED_LOCAL_RE.search(content)
                t0 = parse_iso_ts(m_ca.group(1)) if m_ca else cts
                t1 = parse_iso_ts(m_co.group(1)) if m_co else None
                tdur = (t1 - t0) if (t0 and t1 and t1 >= t0) else None
                first_l = ""
                for l in content.splitlines():
                    if not l.startswith("Created At:") and not l.startswith("Completed At:") and l.strip():
                        first_l = l.strip()
                        break
                timeline_steps.append(
                    {
                        "step_index": s_idx,
                        "kind": "tool",
                        "label": "工具返回",
                        "summary": first_l[:90] or "工具执行完毕",
                        "status": s_status,
                        "created_ts": t0,
                        "duration_sec": round(tdur, 1) if tdur is not None else None,
                        "context_tokens": None,
                        "output_tokens": None,
                        "tok_per_sec": None,
                    }
                )

        elif s_type == "SYSTEM_MESSAGE":
            # 1) 如果客户端/后台服务发生过重启，此前所有 RUNNING 的后台任务已全部终止！
            if "stopped due to server restart" in content:
                for tinfo in bg_tasks.values():
                    if tinfo["status"] == "RUNNING":
                        tinfo["status"] = "STOPPED"
                        tinfo["end_ts"] = cts or now_ts

            # 2) 检查具体后台任务完成消息，并同步解除绑定在该任务上的 TimerCondition 定时器
            finished_senders = set()
            for tid, tinfo in bg_tasks.items():
                if f"/{tid}" in content or f'"{tid}"' in content:
                    tinfo["end_ts"] = cts or now_ts
                    finished_senders.add(tinfo["full_task_id"])
                    finished_senders.add(tid)
                    if "failed" in content.lower() or "exit code: 1" in content.lower():
                        tinfo["status"] = "FAILED"
                    elif "was canceled" in content.lower() or "cancelled" in content.lower():
                        tinfo["status"] = "STOPPED"
                    else:
                        tinfo["status"] = "COMPLETED"
                    timeline_steps.append(
                        {
                            "step_index": s_idx,
                            "kind": "bg_done",
                            "label": f"后台结束 {tid}",
                            "summary": f"{tinfo['title']} 已结束",
                            "status": tinfo["status"],
                            "created_ts": cts,
                            "duration_sec": round(max(0.0, tinfo["end_ts"] - tinfo["start_ts"]), 1),
                            "context_tokens": None,
                            "output_tokens": None,
                            "tok_per_sec": None,
                        }
                    )

            for tinfo in bg_tasks.values():
                if tinfo["status"] == "RUNNING" and tinfo.get("is_timer"):
                    tc = tinfo.get("timer_cond") or ""
                    if tc == "any" or (tc and (tc in finished_senders or tc in content)):
                        tinfo["status"] = "COMPLETED"
                        tinfo["end_ts"] = cts or now_ts

    # 统计同一脚本目录的历史实测完成耗时（仅长于 45s 的真实计算任务，绝不混入 ls/tail 短命令）
    family_durs = {}
    for tinfo in bg_tasks.values():
        if tinfo["status"] == "COMPLETED" and not tinfo.get("is_timer") and tinfo["end_ts"] and tinfo["start_ts"]:
            dur = tinfo["end_ts"] - tinfo["start_ts"]
            fkey = extract_script_family_key(tinfo["command"])
            if fkey and dur >= 45.0:
                family_durs.setdefault(fkey, []).append(dur)

    bg_task_list = []
    running_bg_count = 0
    for tid, tinfo in bg_tasks.items():
        log_fp = os.path.join(tasks_dir, f"{tid}.log")
        tail_txt, log_bytes = read_tail_text(log_fp) if os.path.isfile(log_fp) else ("", 0)
        log_mtime = os.path.getmtime(log_fp) if os.path.isfile(log_fp) else tinfo["start_ts"]
        default_lines = normalize_terminal_lines(tail_txt, max_lines=120)

        is_running = tinfo["status"] == "RUNNING"
        # 一次性定时器到点后若已超过设定时长 + 5 秒，自动归为已完成
        if is_running and tinfo.get("is_timer") and tinfo.get("timer_dur"):
            if (now_ts - tinfo["start_ts"]) > (tinfo["timer_dur"] + 5.0):
                is_running = False
                tinfo["status"] = "COMPLETED"
                tinfo["end_ts"] = tinfo["start_ts"] + tinfo["timer_dur"]

        log_idle_sec = max(0.0, now_ts - log_mtime)
        if is_running and last_ts and (now_ts - last_ts > 7200) and log_idle_sec > 1800:
            is_running = False
            effective_status = "STOPPED"
            elapsed_sec = max(0.0, log_mtime - tinfo["start_ts"])
        elif is_running:
            running_bg_count += 1
            effective_status = "RUNNING"
            elapsed_sec = max(0.0, now_ts - tinfo["start_ts"])
        else:
            effective_status = tinfo["status"]
            end_ref = tinfo["end_ts"] or log_mtime or now_ts
            elapsed_sec = max(0.0, end_ref - tinfo["start_ts"])

        fkey = extract_script_family_key(tinfo["command"])
        hist_list = family_durs.get(fkey) or []
        # 取最近 3 次同类脚本耗时的中位数/均值
        recent_hist = hist_list[-3:]
        hist_script_sec = (sum(recent_hist) / len(recent_hist)) if recent_hist else None

        prog = None
        eta_sec = None
        eta_note = ""
        shown_lines = default_lines[-15:]

        if tinfo.get("is_timer"):
            if tinfo.get("timer_dur"):
                td = tinfo["timer_dur"]
                pct = min(100.0, elapsed_sec * 100.0 / max(1.0, td)) if is_running else 100.0
                eta_sec = round(max(0.0, td - elapsed_sec), 1) if is_running else 0.0
                prog = {
                    "percent": round(pct, 1),
                    "current": None,
                    "total": None,
                    "eta_sec": eta_sec,
                    "rate": f"设定 {int(td)}s",
                    "method": "定时唤醒倒计时",
                }
                eta_note = "定时唤醒器精确倒计时"
        elif tinfo.get("is_daemon"):
            eta_note = "常驻守护服务（持续运行，无固定结束时间）"
        else:
            prog, shown_lines, log_bytes, log_mtime, ext_name = resolve_external_logs_and_progress(
                tinfo["command"],
                tinfo["start_ts"],
                elapsed_sec,
                default_lines,
                log_bytes,
                log_mtime,
                hist_script_sec if is_running else None,
            )
            log_idle_sec = max(0.0, now_ts - (log_mtime or tinfo["start_ts"]))
            if is_running:
                if prog and prog.get("eta_sec") is not None:
                    eta_sec = prog["eta_sec"]
                    eta_note = prog.get("method") or "按实时日志进度推算"
                else:
                    eta_note = "脚本未输出步数/进度，仅精确统计已运行时间"
            else:
                if prog:
                    prog["percent"] = 100.0 if effective_status == "COMPLETED" else prog.get("percent")

        bg_task_list.append(
            {
                "task_id": tid,
                "title": tinfo["title"],
                "command": tinfo["command"],
                "status": effective_status,
                "status_cn": {
                    "RUNNING": "常驻运行" if (is_running and tinfo.get("is_daemon")) else "运行中",
                    "COMPLETED": "已完成",
                    "FAILED": "执行失败",
                    "STOPPED": "已结束",
                }.get(effective_status, effective_status),
                "is_running": is_running,
                "is_daemon": bool(tinfo.get("is_daemon")),
                "is_timer": bool(tinfo.get("is_timer")),
                "start_ts": tinfo["start_ts"],
                "end_ts": tinfo["end_ts"],
                "elapsed_sec": round(elapsed_sec, 1),
                "eta_sec": eta_sec,
                "eta_finish_ts": round(now_ts + eta_sec, 1) if (is_running and eta_sec is not None) else None,
                "eta_note": eta_note,
                "progress": prog,
                "log_bytes": log_bytes,
                "log_idle_sec": round(log_idle_sec, 1),
                "log_tail": shown_lines,
            }
        )

    bg_task_list.sort(
        key=lambda x: (
            0 if x["is_running"] else 1,
            (2 if x.get("is_daemon") else (1 if x.get("is_timer") else 0)) if x["is_running"] else 0,
            -(x["start_ts"] or 0),
        )
    )

    db_map_all = load_official_db_summaries()
    db_info = db_map_all.get(conv_id)
    official_title = (db_info["title"] if db_info else "").strip()
    cascade_running = bool(db_info["is_running"]) if db_info else False

    live_state = "IDLE"
    live_state_cn = "空闲待命"
    live_detail = "等待新的指令输入"
    live_elapsed_sec = 0.0

    non_daemon_running = [t for t in bg_task_list if t["is_running"] and not t["is_daemon"]]

    if raw_steps:
        last_step = raw_steps[-1]
        lt = last_step.get("type")
        l_cts = parse_iso_ts(last_step.get("created_at")) or now_ts
        gap = max(0.0, now_ts - l_cts)

        if non_daemon_running and (lt in ("PLANNER_RESPONSE", "GENERIC", "SYSTEM_MESSAGE")):
            top_bg = non_daemon_running[0]
            live_state = "BG_RUNNING"
            live_state_cn = f"后台任务运行中 ({len(non_daemon_running)} 个)"
            eta_hint = f" · 预计剩余 {int(top_bg['eta_sec'] // 60)}分{int(top_bg['eta_sec'] % 60)}秒" if top_bg["eta_sec"] else ""
            live_detail = f"{top_bg['title']}{eta_hint}"
            live_elapsed_sec = top_bg["elapsed_sec"]
        elif lt == "USER_INPUT" and (cascade_running or gap < 900):
            live_state = "THINKING"
            live_state_cn = "Gemini 思考/生成中"
            live_detail = "已接收用户指令，模型正在深度推理与规划"
            live_elapsed_sec = gap
        elif lt == "PLANNER_RESPONSE" and (last_step.get("tool_calls") or []) and (cascade_running or gap < 900):
            tcs = last_step.get("tool_calls") or []
            tname = tcs[-1].get("name", "tool")
            tsum = clean_unquoted_arg((tcs[-1].get("args") or {}).get("toolSummary", ""))
            live_state = "TOOL_RUNNING"
            live_state_cn = f"正在执行工具: {tname}"
            live_detail = tsum or f"正在等待工具 {tname} 返回结果"
            live_elapsed_sec = gap
        elif lt == "GENERIC" and cascade_running and gap < 600:
            live_state = "THINKING"
            live_state_cn = "Gemini 分析结果中"
            live_detail = "工具已返回，模型正在综合分析下一步"
            live_elapsed_sec = gap

    valid_speeds = [m["tok_per_sec"] for m in planner_metrics if m["tok_per_sec"] is not None]
    valid_durs = [m["duration_sec"] for m in planner_metrics if m["duration_sec"] is not None]
    avg_tok_s = round(sum(valid_speeds) / len(valid_speeds), 1) if valid_speeds else None
    peak_tok_s = round(max(valid_speeds), 1) if valid_speeds else None
    last_tok_s = valid_speeds[-1] if valid_speeds else None
    avg_step_dur = round(sum(valid_durs) / len(valid_durs), 2) if valid_durs else None

    subagents = parse_subagents(conv_dir, now_ts, db_map=db_map_all)

    # 计算真实云端单次请求体估算载荷：压缩后活跃文本 + 全量历史内联图片 Base64（/compact 不会清除已内联图片）
    media_b64_bytes = int(media_raw_bytes * 4 // 3)
    if compact_count > 0 and len(raw_steps) > 0:
        active_compact_bytes = sum(
            len((s.get("content") or "").encode("utf-8", errors="ignore"))
            + len((s.get("thinking") or "").encode("utf-8", errors="ignore"))
            + 256
            for s in raw_steps[last_ckpt_idx:]
        )
        # 按全文未截断比例放大（上限不超过全文体积）
        expansion = min(3.0, max(1.0, full_bytes / max(1, st.st_size)))
        active_text_bytes = min(full_bytes, int(active_compact_bytes * expansion))
    else:
        active_text_bytes = full_bytes
    estimated_payload_bytes = active_text_bytes + media_b64_bytes

    return {
        "server_ts": now_ts,
        "server_version": SERVER_VERSION,
        "conversation_id": conv_id,
        "title": official_title or title or f"会话 {conv_id[:8]}",
        "model": model_name,
        "turn_count": turn_count,
        "step_count": len(raw_steps),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "session_span_sec": round(max(0.0, (last_ts - first_ts)), 1) if (first_ts and last_ts) else 0.0,
        "live": {
            "state": live_state,
            "state_cn": live_state_cn,
            "detail": live_detail,
            "elapsed_sec": round(live_elapsed_sec, 1),
            "current_turn_elapsed_sec": round(max(0.0, now_ts - last_user_turn_ts), 1)
            if (live_state != "IDLE" and last_user_turn_ts)
            else 0.0,
        },
        "context": {
            "current_tokens": latest_context_tokens,
            "latest_input_tokens": latest_input_tokens,
            "latest_output_tokens": latest_output_tokens,
            "cache_hit_pct": round(latest_cache_ratio, 1),
            "cumulative_cache_hit_pct": round(
                (total_cache_tokens * 100.0 / (total_input_tokens + total_cache_tokens))
                if (total_input_tokens + total_cache_tokens) > 0
                else 0.0,
                1,
            ),
            "transcript_bytes": st.st_size,
            "transcript_full_bytes": full_bytes,
            "active_text_bytes": active_text_bytes,
            "media_count": len(seen_media_paths),
            "media_b64_bytes": media_b64_bytes,
            "compact_count": compact_count,
            "estimated_payload_bytes": estimated_payload_bytes,
            "payload_limit_bytes": PAYLOAD_HARD_LIMIT_BYTES,
            "payload_pct_30mb": round(estimated_payload_bytes * 100.0 / PAYLOAD_HARD_LIMIT_BYTES, 2),
        },
        "tokens": {
            "total_uncached_input": total_input_tokens,
            "total_cache_read": total_cache_tokens,
            "total_output": total_output_tokens,
            "total_billed_io": total_input_tokens + total_output_tokens,
            "total_processed_all": total_input_tokens + total_cache_tokens + total_output_tokens,
        },
        "speed": {
            "last_tok_per_sec": last_tok_s,
            "avg_tok_per_sec": avg_tok_s,
            "peak_tok_per_sec": peak_tok_s,
            "avg_step_duration_sec": avg_step_dur,
            "planner_calls": len(planner_metrics),
        },
        "series": planner_metrics[-40:],
        "background_tasks": bg_task_list,
        "running_bg_count": len(non_daemon_running),
        "subagents": subagents,
        "timeline": timeline_steps[-50:],
    }


_HTTP_SERVER_REF = None


class MonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/ping":
            self._send_json({"ok": True, "version": SERVER_VERSION, "pid": os.getpid()})
            return

        if path == "/api/shutdown":
            self._send_json({"ok": True, "shutting_down": True})
            if _HTTP_SERVER_REF is not None:
                threading.Thread(target=_HTTP_SERVER_REF.shutdown, daemon=True).start()
            return

        if path in ("/", "/index.html"):
            try:
                with open(INDEX_HTML, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if path == "/api/overview":
            convs = list_conversations(limit=35)
            req_cid = (qs.get("conv") or [""])[0].strip()
            auto_follow = (qs.get("auto") or ["1"])[0].strip() != "0"

            active_cid = None
            if not auto_follow and req_cid and UUID_RE.match(req_cid):
                active_cid = req_cid
            elif convs:
                # 自动跟随时优先挑选正在运行中的最新会话
                running_convs = [c for c in convs if c.get("is_running")]
                active_cid = running_convs[0]["id"] if running_convs else convs[0]["id"]
            elif req_cid:
                active_cid = req_cid

            detail = get_conversation_detail(active_cid) if active_cid else None
            self._send_json(
                {
                    "server_version": SERVER_VERSION,
                    "brain_dir": BRAIN_DIR,
                    "conversations": convs,
                    "active_conversation_id": active_cid,
                    "detail": detail,
                }
            )
            return

        self._send_json({"error": "Not found"}, status=404)


def ensure_http_server_loop(host, port):
    """后台守护线程：确保本地 HTTP 监控端口始终在线，若旧版本占口则平滑热替换，多窗口时自动热备接管"""
    global _HTTP_SERVER_REF
    base_url = f"http://{host}:{port}"
    while True:
        try:
            with urllib.request.urlopen(f"{base_url}/api/ping", timeout=1.2) as resp:
                info = json.loads(resp.read().decode("utf-8"))
                if info.get("version") != SERVER_VERSION:
                    try:
                        urllib.request.urlopen(f"{base_url}/api/shutdown", timeout=1.2).read()
                    except Exception:
                        pass
                    time.sleep(0.4)
                else:
                    # 已有同版本健康实例在线，本进程静默热备，每 4 秒巡检一次
                    time.sleep(4.0)
                    continue
        except Exception:
            pass

        try:
            srv = ThreadingHTTPServer((host, port), MonitorHandler)
            _HTTP_SERVER_REF = srv
            srv.serve_forever()
        except OSError:
            time.sleep(3.0)
        except Exception:
            time.sleep(3.0)


def run_mcp_stdio_loop():
    """
    轻量级零工具 MCP Stdio 握手循环：
    由 Antigravity Language Server 随客户端启动自动拉起、重启后自动恢复；
    tools/list 返回空数组，对大模型上下文实现 0 Token 零污染。
    同时兼容换行分隔 JSON-RPC (标准 MCP) 与 Content-Length 头帧格式。
    """
    in_stream = sys.stdin.buffer
    out_stream = sys.stdout.buffer

    def send_msg(payload_obj, use_framing=False):
        raw = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
        if use_framing:
            hdr = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
            out_stream.write(hdr + raw)
        else:
            out_stream.write(raw + b"\n")
        out_stream.flush()

    while True:
        line = in_stream.readline()
        if not line:
            break
        s = line.strip()
        if not s:
            continue

        use_cl = False
        if s.lower().startswith(b"content-length:"):
            use_cl = True
            cl = int(s.split(b":", 1)[1].strip())
            while True:
                hline = in_stream.readline()
                if not hline or hline in (b"\r\n", b"\n"):
                    break
            body = in_stream.read(cl)
        else:
            body = s

        try:
            req = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            continue

        method = req.get("method", "")
        req_id = req.get("id", None)
        if req_id is None:
            # 通知类消息无需响应
            continue

        if method == "initialize":
            params = req.get("params") or {}
            proto = params.get("protocolVersion") or "2024-11-05"
            res = {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ag-monitor", "version": SERVER_VERSION},
            }
            send_msg({"jsonrpc": "2.0", "id": req_id, "result": res}, use_cl)
        elif method == "tools/list":
            send_msg({"jsonrpc": "2.0", "id": req_id, "result": {"tools": []}}, use_cl)
        elif method == "resources/list":
            send_msg({"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}, use_cl)
        elif method == "prompts/list":
            send_msg({"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}, use_cl)
        elif method == "ping":
            send_msg({"jsonrpc": "2.0", "id": req_id, "result": {}}, use_cl)
        else:
            send_msg(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                },
                use_cl,
            )


def set_low_priority():
    try:
        import ctypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS
        )
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="AGY 中文实时任务与上下文监控台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="监听端口 (默认 8765)")
    parser.add_argument("--mcp", action="store_true", help="以 AGY MCP 自启动守护模式运行")
    args = parser.parse_args()

    set_low_priority()

    if args.mcp:
        # MCP 模式下严格保持 stdout 纯净仅输出 JSON-RPC
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        t = threading.Thread(target=ensure_http_server_loop, args=(args.host, args.port), daemon=True)
        t.start()
        run_mcp_stdio_loop()
        return

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    print(f"[AGY 监控台 v{SERVER_VERSION}] 正在启动: http://{args.host}:{args.port}")
    sys.stdout.flush()
    ensure_http_server_loop(args.host, args.port)


if __name__ == "__main__":
    main()
