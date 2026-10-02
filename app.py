import asyncio
import ctypes
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import datetime as dt
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from playwright.async_api import async_playwright

APP_NAME = "인트리홀딩스 출근 자동 체크"
APP_VERSION = "1.1.0"
CREDIT = "만든이: 도형이형"
GITHUB_REPO = "raindevilrain-hub/intriholdings-attendance"  # owner/repo - 깃헙 릴리스에서 최신 버전을 확인한다
# HTTP 헤더는 latin-1만 허용되어 한글 APP_NAME을 그대로 쓰면 UnicodeEncodeError가 난다.
UPDATE_USER_AGENT = "IntriHoldingsAttendance-UpdateChecker"

# 카드-온-그레이 대비 없이 창 전체를 흰 배경 하나로 통일하고(COLOR_BG == COLOR_CARD),
# 포인트 컬러도 채도를 낮춰서 하이웍스 로그인 화면처럼 플랫한 톤으로 맞춘다.
COLOR_BG = "#FFFFFF"
COLOR_CARD = "#FFFFFF"
COLOR_TEXT = "#111827"
COLOR_SUBTEXT = "#6B7280"
COLOR_HINT = "#9CA3AF"
COLOR_ACCENT = "#3B6597"
COLOR_ACCENT_DARK = "#2D4E77"
COLOR_BORDER = "#D9DCE3"
COLOR_SUCCESS = "#16A34A"
COLOR_DANGER = "#DC2626"

def _app_root(var):
    """환경변수가 없거나 비어 있으면 홈 폴더로 떨어뜨린다 (상대경로가 되면 삭제가 위험해진다)."""
    return Path(os.getenv(var) or Path.home())

APP_DIR = _app_root("APPDATA") / "IntriHoldingsAttendance"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG = APP_DIR / "config.json"
PROFILE = APP_DIR / "browser-profile"
STATE = APP_DIR / "state.json"
SESSION_FILE = APP_DIR / "session.dat"
CREDS_FILE = APP_DIR / "creds.dat"
URL = "https://login.office.hiworks.com/intriholdings.com"
# 예전 AHK 스크립트가 실제로 쓰던 주소. 로그인 상태면 여기서 바로 출퇴근 버튼이 보이고,
# 아니면 로그인 화면으로 리디렉트된다 (로그인 URL로 들어가서 리디렉트를 기대하는 것보다 안전).
DASHBOARD_URL = "https://dashboard.office.hiworks.com/"
LOGIN_MARKER = "login.office.hiworks.com"
COOKIE_DOMAIN_MARKER = "hiworks"
DEBUG_DIR = APP_DIR / "debug"

DEFAULT = {
    "shutdown_delay": 2,
    "office_public_ip": None,
    "setup_complete": False,
    "admin_pin_salt": None,
    "admin_pin_hash": None,
    "debug_show_browser": False,
}


# ---------- 설정/상태 파일 ----------

def load_config():
    c = DEFAULT.copy()
    if CONFIG.exists():
        try: c.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except: pass
    return c

def save_config(c):
    CONFIG.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")

def load_state():
    try: return json.loads(STATE.read_text(encoding="utf-8"))
    except: return {}

def save_state(s):
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

def already_checked_in_today():
    return load_state().get("checkin_date") == dt.date.today().isoformat()

def mark_checkin():
    s = load_state(); s["checkin_date"] = dt.date.today().isoformat(); save_state(s)

def checkin_already_asked_today():
    """출근 여부가 모호해서 한 번 물어봤으면(답이 뭐였든) 오늘은 더 묻지/재시도하지 않는다."""
    return load_state().get("checkin_asked_date") == dt.date.today().isoformat()

def mark_checkin_asked():
    s = load_state(); s["checkin_asked_date"] = dt.date.today().isoformat(); save_state(s)

def update_already_checked_today():
    return load_state().get("update_checked_date") == dt.date.today().isoformat()

def mark_update_checked():
    s = load_state(); s["update_checked_date"] = dt.date.today().isoformat(); save_state(s)

def already_checked_out_today():
    return load_state().get("checkout_date") == dt.date.today().isoformat()

def mark_checkout():
    s = load_state(); s["checkout_date"] = dt.date.today().isoformat(); save_state(s)


# ---------- 사내망 판별 ----------
# ponytail: 공인 IP 단순 비교(회선 이중화/유동IP 변경 시 재등록 필요). 필요해지면 사내 서버 접속확인 방식을 병행.

IP_ENDPOINTS = ["https://api.ipify.org", "https://checkip.amazonaws.com", "https://icanhazip.com"]

# 프록시를 타면 회사 밖에서도 회사 공인 IP가 보일 수 있다(= 사내망 제한이 무력화된다).
# 시스템/환경변수 프록시 설정을 무시하고 직접 나가도록 고정한 opener를 쓴다.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def get_public_ip(timeout=5, max_endpoints=None):
    """공인 IP 조회. 한 서비스가 죽어 있어도 되도록 여러 곳을 순서대로 시도하고,
    IP 형식이 아닌 응답(프록시/차단 안내 페이지 등)은 버린다."""
    for url in IP_ENDPOINTS[:max_endpoints]:
        try:
            with _NO_PROXY_OPENER.open(url, timeout=timeout) as r:
                text = r.read().decode().strip()
            return str(ipaddress.ip_address(text))
        except Exception:
            continue
    return None

def check_network(cfg, quick=False):
    """사내망 여부를 'yes' / 'no' / 'unknown'(조회 실패)로 구분해서 돌려준다.
    quick=True는 종료 시점용: Windows는 종료 질의에 5초 넘게 답이 없으면 '응답 없음' 화면을
    띄우므로, 조회를 최대 2곳/2초로 제한한다."""
    office_ip = cfg.get("office_public_ip")
    if not office_ip:
        return "no"
    current = get_public_ip(timeout=2, max_endpoints=2) if quick else get_public_ip()
    if current is None:
        return "unknown"   # 인터넷이 잠깐 끊겼을 뿐일 수 있다 -> 영구 판단(래치)하면 안 된다
    return "yes" if current == office_ip else "no"

def is_company_network(cfg, quick=False):
    return check_network(cfg, quick=quick) == "yes"


# ---------- 하이웍스 자동화 ----------

async def save_debug_snapshot(page, label):
    """자동화가 실패했을 때 그 순간의 화면을 남겨서 원인 파악을 쉽게 한다."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = DEBUG_DIR / f"{label}_{stamp}.png"
        await page.screenshot(path=str(path), full_page=True)
        old = sorted(DEBUG_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)[10:]
        for f in old:      # 최근 10장만 남긴다
            try: f.unlink()
            except Exception: pass
        return path
    except Exception:
        return None

async def find_and_click(page, texts):
    for text in texts:
        locs = [
            page.get_by_role("button", name=text, exact=True),
            page.get_by_text(text, exact=True),
            page.locator(f'button:has-text("{text}")'),
            page.locator(f'a:has-text("{text}")')
        ]
        for loc in locs:
            try:
                n = await loc.count()
                for i in range(min(n, 8)):
                    x = loc.nth(i)
                    # 비활성 버튼(입력값이 검증 통과 전인 '다음' 등)은 5초씩 기다리지 말고 건너뛴다.
                    if await x.is_visible() and await x.is_enabled():
                        await x.scroll_into_view_if_needed()
                        await x.click(timeout=5000)
                        return True
            except: pass
    return False

def _host_of(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""

def _is_hiworks_host(url):
    """하이웍스 도메인인지 호스트명으로 정확히 확인한다. url 안에 문자열이 들어있는지만 보면
    'https://나쁜사이트/?x=login.office.hiworks.com' 같은 주소도 통과해버린다."""
    h = _host_of(url)
    return h == "hiworks.com" or h.endswith(".hiworks.com")

def _on_login_page(page):
    return _host_of(page.url) == LOGIN_MARKER


# ---------- 로그인 세션 보관 (Windows DPAPI 암호화) ----------
# 실험으로 확인: 만료시간 없는 "세션 쿠키"는 Edge를 닫으면 사라진다. 하이웍스가 세션 쿠키로
# 인증하면 비밀번호를 저장하지 않는 이 앱은 둘째 날부터 로그인이 풀리므로, 실행이 끝날 때
# 쿠키를 꺼내 DPAPI(현재 Windows 사용자만 복호화 가능)로 암호화해 저장하고 다음 실행 때 복원한다.

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

def _dpapi(data, encrypt):
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    fn.restype = wintypes.BOOL
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    if encrypt:
        fn.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    else:
        fn.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI 처리 실패")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))

def save_credentials(username, password):
    """하이웍스 아이디/비밀번호를 DPAPI로 암호화해 저장한다. 이 PC의 이 Windows 계정으로만
    복호화된다. 저장해 두면 세션이 만료돼도 프로그램이 알아서 다시 로그인한다."""
    CREDS_FILE.write_bytes(_dpapi(json.dumps({"u": username, "p": password}).encode("utf-8"), True))

def load_credentials():
    try:
        d = json.loads(_dpapi(CREDS_FILE.read_bytes(), False).decode("utf-8"))
        return d.get("u"), d.get("p")
    except Exception:
        return None, None

def has_credentials():
    return CREDS_FILE.exists() and load_credentials()[0] is not None

def clear_credentials():
    try: CREDS_FILE.unlink(missing_ok=True)
    except Exception: pass

def _write_session_cookies(cookies):
    SESSION_FILE.write_bytes(_dpapi(json.dumps(cookies).encode("utf-8"), True))

def _read_session_cookies():
    try:
        return json.loads(_dpapi(SESSION_FILE.read_bytes(), False).decode("utf-8"))
    except Exception:
        return []

async def _load_session_cookies(context):
    cookies = _read_session_cookies()
    if cookies:
        try: await context.add_cookies(cookies)
        except Exception: pass

async def _save_session_cookies(context):
    try:
        cookies = [c for c in await context.cookies() if COOKIE_DOMAIN_MARKER in c.get("domain", "")]
        if cookies:
            _write_session_cookies(cookies)
    except Exception:
        pass

async def _find_text_field(page):
    selectors = [
        'input[type="email"]:visible',
        'input[name*="email" i]:visible',
        'input[name*="user" i]:visible',
        'input[name*="id" i]:visible',
        'input[type="text"]:visible',
        'input:not([type]):visible',  # type 속성 자체가 없는 입력창 (HTML 기본값은 text지만 속성 선택자는 못 잡음)
    ]
    for sel in selectors:
        x = page.locator(sel).first
        if await x.count():
            return x
    # 최후 수단: 화면에 보이는 입력창 중 비밀번호/체크박스 등이 아닌 첫 번째
    try:
        candidates = page.locator("input")
        n = await candidates.count()
        for i in range(min(n, 8)):
            cand = candidates.nth(i)
            if not await cand.is_visible():
                continue
            t = (await cand.get_attribute("type")) or "text"
            if t.lower() not in ("password", "hidden", "checkbox", "radio", "submit", "button"):
                return cand
    except Exception:
        pass
    return None

PW_SELECTOR = 'input[type="password"]:visible'

async def _wait_for_password_field(page, tries=10, interval_ms=300):
    for _ in range(tries):
        if await page.locator(PW_SELECTOR).count():
            return True
        await page.wait_for_timeout(interval_ms)
    return False

async def _grab_login_error(page):
    """로그인 실패 시 화면에 뜨는 실제 에러 메시지를 최대한 그대로 잡아온다."""
    try:
        for sel in ['[class*="error" i]', '[class*="invalid" i]', '[role="alert"]']:
            loc = page.locator(sel)
            n = await loc.count()
            for i in range(min(n, 5)):
                el = loc.nth(i)
                if not await el.is_visible():
                    continue
                t = (await el.inner_text()).strip()
                if t and len(t) < 200:
                    return t
    except Exception:
        pass
    return None

def normalize_login_id(username):
    """하이웍스 로그인창은 '@intriholdings.com'을 고정으로 붙여 보여주고 입력칸에는 앞부분만
    받는다(소문자/숫자/-_.만 허용). 전체 이메일로 입력해도 되도록 '@' 뒤는 떼고 소문자로 맞춘다."""
    return username.strip().split("@")[0].strip().lower()

async def fill_login_if_present(page, username, password):
    """하이웍스는 아이디 입력 -> 다음 -> 비밀번호 입력의 2단계 로그인이다.
    (한 화면에 아이디+비밀번호가 같이 있는 경우도 대비해서 둘 다 처리한다.)
    성공하면 None, 실패하면 사용자에게 보여줄 사유 문자열을 돌려준다."""
    # 캡티브 포털이나 중간에 낚아챈 페이지에 비밀번호를 흘리지 않도록, 하이웍스 도메인이
    # 확실할 때만 입력한다.
    if not _is_hiworks_host(page.url):
        return f"하이웍스 주소가 아니어서 로그인을 중단했습니다: {_host_of(page.url) or '(알 수 없음)'}"
    try:
        has_pw = await page.locator(PW_SELECTOR).count() > 0

        if not has_pw:
            id_field = await _find_text_field(page)
            if id_field is None:
                return "로그인 입력창을 찾지 못했습니다. 브라우저에서 직접 로그인해 주세요."
            await id_field.fill(normalize_login_id(username))
            await page.wait_for_timeout(300)
            if not await find_and_click(page, ["다음", "Next", "확인"]):
                sub = page.locator('button[type="submit"], input[type="submit"]').first
                if await sub.count() and await sub.is_enabled():
                    await sub.click()
                else:
                    err = await _grab_login_error(page)
                    return f"아이디를 확인해 주세요: {err}" if err else "아이디 다음 단계로 넘어가지 못했습니다. 아이디를 확인해 주세요."
            if not await _wait_for_password_field(page):
                err = await _grab_login_error(page)
                return f"아이디를 확인해 주세요: {err}" if err else "비밀번호 입력 화면이 나타나지 않았습니다. 아이디를 확인해 주세요."

        pw = page.locator(PW_SELECTOR).first
        if await pw.count() == 0:
            return "비밀번호 입력창을 찾지 못했습니다."
        await pw.fill(password)
        if not await find_and_click(page, ["로그인", "Login", "Sign in", "로그인하기", "다음", "확인"]):
            sub = page.locator('button[type="submit"], input[type="submit"]').first
            if await sub.count() and await sub.is_enabled():
                await sub.click()
            else:
                return "로그인 버튼을 누르지 못했습니다."
        await page.wait_for_timeout(1500)
        return None
    except Exception as e:
        return f"로그인 입력 중 오류: {e}"

def _ask_already_checked_in():
    """화면만으로는 '오늘 이미 출근함'과 '어제 퇴근 누락'을 구분할 수 없다.
    부팅 자동 체크(사람이 없을 수도 있음)에서도 물어보므로, 응답이 없으면 알아서 닫혀야 한다."""
    r = _msgbox_timeout(
        "하이웍스가 이미 '출근' 상태로 보입니다 (퇴근하기 버튼만 있음).\n\n"
        "오늘 출근 체크를 이미 하셨나요?\n\n"
        "예    → 오늘 출근 완료로 기록합니다\n"
        "아니오 → 어제 퇴근 체크가 빠진 것일 수 있으니 하이웍스에서 직접 확인하세요\n\n"
        f"{CONFIRM_TIMEOUT_MS // 1000}초간 응답이 없으면 '아니오'로 처리합니다.",
        APP_NAME, MB_YESNO | MB_ICONQUESTION, CONFIRM_TIMEOUT_MS)
    return r == IDYES

def _ask_already_checked_out():
    return ctypes.windll.user32.MessageBoxW(
        None,
        "하이웍스 화면에 출근/퇴근 버튼이 모두 없습니다.\n\n"
        "오늘 퇴근 체크를 이미 하셨나요?\n\n"
        "예    → 오늘 퇴근 완료로 기록합니다\n"
        "아니오 → 하이웍스에서 직접 확인해 주세요",
        APP_NAME, MB_YESNO | MB_ICONQUESTION) == IDYES

async def _page_text(page):
    return await page.locator("body").inner_text()

async def _snapshot_note(page, label):
    shot = await save_debug_snapshot(page, label)
    return f" (화면 저장됨: {shot.name})" if shot else ""

async def _do_checkin(page, interactive=False):
    body = await _page_text(page)
    if "퇴근하기" in body and "출근하기" not in body:
        if already_checked_in_today():
            return True, "오늘은 이미 출근 처리되어 있습니다."
        # 오늘 기록이 없는데 '퇴근하기'만 떠 있다. 두 가지가 같은 화면으로 보인다:
        #  (1) 오늘 이미 손으로 출근 체크를 했다  <- 설치 첫날에 아주 흔하다
        #  (2) 어제 퇴근 체크가 빠져서 버튼이 그대로 남아 있다
        # 화면만으로는 구분할 수 없으니, 사람이 앞에 있을 때는 물어보고,
        # 자동 실행(부팅)일 때는 함부로 기록하지 않는다.
        if interactive:
            mark_checkin_asked()  # 답이 뭐였든 오늘은 이 질문을 다시 띄우지 않는다
            if _ask_already_checked_in():
                mark_checkin()
                return True, "이미 출근 상태로 확인되어, 오늘 출근 완료로 기록했습니다."
        shot = await save_debug_snapshot(page, "checkin_stale_state")
        note = f" (화면 저장됨: {shot.name})" if shot else ""
        return False, ("출근 버튼이 없고 퇴근 버튼만 있습니다. 어제 퇴근 체크가 빠졌을 수 있으니 "
                       "하이웍스에서 직접 확인해 주세요." + note)
    # 정확히 '출근하기' 버튼만 누른다 (예전 AHK 스크립트와 동일). '출근' 같은 느슨한 글자는
    # 메뉴/라벨을 잘못 누를 수 있어서 쓰지 않는다 - 못 찾으면 안전하게 실패하고 화면을 남긴다.
    ok = await find_and_click(page, ["출근하기"])
    if not ok:
        await page.wait_for_timeout(2500)
        ok = await find_and_click(page, ["출근하기"])
    if not ok:
        return False, "출근 버튼을 찾지 못했습니다." + await _snapshot_note(page, "checkin_fail")
    await page.wait_for_timeout(1200)
    if "퇴근하기" in await _page_text(page):
        mark_checkin()
        return True, "출근 체크가 완료되었습니다."
    return False, "출근 버튼은 눌렀지만 완료 상태를 확인하지 못했습니다." + await _snapshot_note(page, "checkin_unconfirmed")

async def _do_checkout(page, interactive=False):
    body = await _page_text(page)
    has_out = "퇴근하기" in body
    if not has_out:
        if "출근하기" in body:
            return False, "오늘 출근 기록이 없어 퇴근 체크를 할 수 없습니다."
        # 출근/퇴근 버튼이 둘 다 없다 = 이미 퇴근했을 수도, 화면이 제대로 안 그려졌을 수도 있다.
        # 사람이 앞에 있으면 물어보고, 자동 실행이면 거짓으로 기록하지 않는다.
        if interactive and _ask_already_checked_out():
            mark_checkout()
            return True, "이미 퇴근 상태로 확인되어, 오늘 퇴근 완료로 기록했습니다."
        shot = await save_debug_snapshot(page, "checkout_unknown")
        note = f" (화면 저장됨: {shot.name})" if shot else ""
        return False, ("퇴근 버튼이 보이지 않아 퇴근 여부를 확인하지 못했습니다. "
                       "하이웍스에서 직접 확인해 주세요." + note)
    ok = await find_and_click(page, ["퇴근하기"])
    if not ok:
        await page.wait_for_timeout(2500)
        ok = await find_and_click(page, ["퇴근하기"])
    if not ok:
        return False, "퇴근 버튼을 찾지 못했습니다." + await _snapshot_note(page, "checkout_fail")
    # 클릭 뒤 '퇴근하기' 버튼이 사라져야 성공으로 본다 (확인창이 떴거나 클릭이 안 먹힌 경우를 걸러냄).
    for _ in range(10):
        await page.wait_for_timeout(500)
        if "퇴근하기" not in await _page_text(page):
            mark_checkout()
            return True, "퇴근 체크가 완료되었습니다."
    return False, "퇴근 버튼은 눌렀지만 완료 상태를 확인하지 못했습니다." + await _snapshot_note(page, "checkout_unconfirmed")

async def _ensure_logged_in(page, username, password):
    """대시보드로 들어가 로그인 상태를 확인하고, 필요하면 로그인한다. 실패 사유 문자열 또는 None."""
    await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1000)
    if not _on_login_page(page):
        return None

    if not (username and password):
        # 저장된 아이디/비밀번호가 있으면 알아서 다시 로그인한다 (세션 만료 시 수동 개입 불필요).
        username, password = load_credentials()
    if not (username and password):
        return "로그인이 필요합니다. 프로그램을 열어 하이웍스 아이디/비밀번호를 등록해 주세요."

    await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(800)
    fail = await fill_login_if_present(page, username, password)
    if fail:
        return fail

    # 로그인 처리(리디렉트)가 끝날 때까지 최대 10초 기다린다.
    for _ in range(20):
        if not _on_login_page(page):
            break
        await page.wait_for_timeout(500)
    if _on_login_page(page):
        err = await _grab_login_error(page)
        if err:
            return f"로그인에 실패했습니다: {err}"
        return "추가 인증/로그인이 필요합니다. 브라우저에서 로그인 후 다시 시도해 주세요."

    await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1000)
    if _on_login_page(page):
        return "로그인 세션이 유지되지 않았습니다."
    return None

async def _dismiss_popups(page):
    """하이웍스는 대시보드에 들어갈 때마다 'AI채팅' 등 홍보 팝업을 띄운다. 이게 근무체크
    버튼 위를 덮고 있으면 Playwright의 클릭이 막힌다(요소는 있어도 가려져서 못 누름).
    특정 문구에 의존하지 않고 Esc + 흔한 닫기 버튼 패턴으로 범용적으로 닫는다.
    (실사용 중 실제로 이 팝업 때문에 '이미 출근한 것처럼' 보이는 오탐이 40분 넘게 반복된 적이 있다.)"""
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
    except Exception:
        pass
    for sel in ['button[aria-label="닫기"]', 'button[aria-label="Close"]',
                '[role="dialog"] button:has-text("닫기")', '[role="dialog"] button:has-text("×")']:
        try:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=1000)
                await page.wait_for_timeout(200)
                break
        except Exception:
            pass

async def attend(mode, username=None, password=None, headless=True, interactive=False):
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch_persistent_context(
                str(PROFILE), channel="msedge", headless=headless,
                viewport={"width": 1400, "height": 900}, locale="ko-KR",
                args=["--disable-blink-features=AutomationControlled"]
            )
        except Exception as e:
            if "msedge" in str(e) and ("not found" in str(e) or "Executable doesn't exist" in str(e)):
                return False, "Microsoft Edge를 찾을 수 없습니다. Edge를 설치한 뒤 다시 시도해 주세요."
            return False, f"오류: {e}"
        page = browser.pages[0] if browser.pages else await browser.new_page()
        logged_in = False
        try:
            await _load_session_cookies(browser)

            fail = await _ensure_logged_in(page, username, password)
            if fail:
                return False, fail
            logged_in = True

            # 대시보드 위젯이 그려질 시간을 넉넉히 준다 (SPA 렌더링 지연 대응).
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            await _dismiss_popups(page)

            if mode == "checkin":
                return await _do_checkin(page, interactive)
            if mode == "checkout":
                return await _do_checkout(page, interactive)
            return False, f"알 수 없는 동작: {mode}"
        except Exception as e:
            return False, f"오류: {e}"
        finally:
            if logged_in:
                await _save_session_cookies(browser)
            await browser.close()

_ATTEND_LOCK = threading.Lock()

# 이름을 상수로 빼둔 이유: 테스트가 실제 설치본과 같은 프로세스 전역 뮤텍스를 잡으면 서로
# 충돌한다(테스트는 파일 경로를 임시 폴더로 돌리지만, Win32 이름 있는 뮤텍스는 경로와 무관하게
# 세션 전체에서 하나뿐이다). 테스트에서 이 값을 갈아끼워 실제 설치본과 분리할 수 있게 한다.
BROWSER_MUTEX_NAME = "Local\\IntriHoldingsAttendanceBrowserMutex"
WATCHER_MUTEX_NAME = "Local\\IntriHoldingsAttendanceWatcherMutex"

class _BrowserLock:
    """브라우저 프로필/세션 파일을 동시에 쓰지 못하게 막는다. 스레드(한 프로세스 안)뿐 아니라
    프로세스 사이에서도 막아야 한다 - 백그라운드 감시기가 출근 체크를 재시도하는 동안 사용자가
    상태 창에서 '지금 퇴근 체크'를 누르면 서로 다른 프로세스가 같은 프로필을 연다."""

    def __enter__(self):
        _ATTEND_LOCK.acquire()
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.k.CreateMutexW.restype = ctypes.c_void_p
        self.k.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self.k.ReleaseMutex.argtypes = [ctypes.c_void_p]
        self.k.CloseHandle.argtypes = [ctypes.c_void_p]
        self.h = self.k.CreateMutexW(None, False, BROWSER_MUTEX_NAME)
        self.held = False
        if self.h:
            # 최대 3분 대기. 앞의 작업이 그보다 오래 걸리면 그냥 진행한다(멈춰 있는 것보다 낫다).
            self.held = self.k.WaitForSingleObject(self.h, 180000) in (0, 0x80)
        return self

    def __exit__(self, *exc):
        try:
            if self.h:
                if self.held:
                    self.k.ReleaseMutex(self.h)
                self.k.CloseHandle(self.h)
        finally:
            _ATTEND_LOCK.release()
        return False

def run_attend(mode, username=None, password=None, headless=True, interactive=False):
    """interactive=True 는 '지금 사람이 화면 앞에 있다'는 뜻. 화면만으로 판단이 안 되는
    애매한 상황에서 사용자에게 직접 물어봐도 되는 경우에만 켠다."""
    with _BrowserLock():
        return asyncio.run(attend(mode, username, password, headless=headless, interactive=interactive))

def shutdown_now(delay=2):
    os.system(f"shutdown /s /t {int(delay)}")

MB_YESNO = 0x00000004
MB_ICONQUESTION = 0x00000020
MB_ICONWARNING = 0x00000030
MB_ICONINFORMATION = 0x00000040
MB_SYSTEMMODAL = 0x00001000
MB_SETFOREGROUND = 0x00010000
MB_TOPMOST = 0x00040000
IDYES = 6

CONFIRM_TIMEOUT_MS = 20000
IDTIMEOUT = 32000

def _confirm_checkout_dialog(timeout_ms=CONFIRM_TIMEOUT_MS):
    """퇴근 체크가 안 된 채 종료하려 할 때 붙잡는 팝업.
    "yes"   = 퇴근 체크하고 종료
    "no"    = 지금은 종료하지 않음 (창을 닫고 자리로 돌아감)
    "away"  = 시간 안에 응답 없음. 자리에 사람이 없다는 뜻이므로 종료를 막지 않는다
              (예약 재부팅/원격 종료가 밤새 취소되면 더 곤란하다)."""
    text = ("아직 퇴근 체크가 되어 있지 않습니다.\n\n"
            "예 → 하이웍스 퇴근 체크를 하고 컴퓨터를 종료합니다\n"
            "아니오 → 종료를 취소합니다 (직접 퇴근 체크 후 다시 종료하세요)\n\n"
            f"{timeout_ms // 1000}초간 응답이 없으면 그대로 종료합니다.")
    r = _msgbox_timeout(text, "퇴근 체크를 먼저 해주세요", MB_YESNO | MB_ICONQUESTION, timeout_ms)
    if r == IDYES:
        return "yes"
    if r == IDTIMEOUT:
        return "away"
    return "no"

def _msgbox_timeout(text, title, flags, timeout_ms=CONFIRM_TIMEOUT_MS):
    """종료 처리 중에 띄우는 창은 반드시 스스로 닫혀야 한다. 자리에 사람이 없으면(예약 재부팅 등)
    종료 화면 뒤에 가려진 팝업 때문에 종료가 밤새 멈춘다.
    TOPMOST/SETFOREGROUND가 없으면 다른 창(브라우저 등)에 가려져 사람이 안 보는 사이
    타임아웃으로 조용히 넘어가버린다 - 그래서 여기서 무조건 최상단으로 띄운다."""
    flags = flags | MB_SYSTEMMODAL | MB_TOPMOST | MB_SETFOREGROUND
    try:
        fn = ctypes.windll.user32.MessageBoxTimeoutW
        fn.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.LPCWSTR,
                       ctypes.c_uint, wintypes.WORD, wintypes.DWORD]
        return fn(None, text, title, flags, 0, timeout_ms)
    except Exception:
        return ctypes.windll.user32.MessageBoxW(None, text, title, flags)

def _notify_checkout_success(info):
    # 성공은 실패보다 훨씬 짧게 보여준다 - "됐다"는 확인만 주고 종료를 오래 붙잡지 않는다.
    _msgbox_timeout(
        f"{info}\n\n잠시 후 컴퓨터가 종료됩니다.",
        "퇴근 체크 완료", MB_ICONINFORMATION, timeout_ms=4000)

def _notify_checkout_failed(info):
    _msgbox_timeout(
        f"퇴근 체크에 실패했습니다.\n사유: {info}\n\n"
        "컴퓨터는 그대로 종료됩니다. 하이웍스에서 직접 확인해 주세요.",
        "퇴근 체크 실패", MB_ICONWARNING)

def _notify_checkin_failed(info):
    # 이 알림도 스스로 닫혀야 한다: 백그라운드 스레드가 여기서 멈춰 있으면 날짜가 바뀌어도
    # 다음 출근 체크를 시작하지 못한다.
    _msgbox_timeout(
        f"자동 출근 체크에 실패했습니다.\n사유: {info}\n\n"
        "프로그램을 다시 열어 수동으로 체크해 주세요.",
        "출근 체크 실패", MB_ICONWARNING, timeout_ms=120000)

# ---------- 트레이 아이콘 ----------
# 감시기(watch)는 이미 숨은 창 + 메시지 루프를 갖고 있어서, 거기에 트레이 아이콘을 붙인다.
# 별도 라이브러리 없이 Shell_NotifyIconW 만 쓴다.

WM_TRAY = 0x0400 + 1          # WM_APP+1: 트레이 아이콘이 클릭을 알려오는 메시지
TRAY_UID = 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
MENU_CHECKIN, MENU_CHECKOUT, MENU_OPEN, MENU_QUIT = 101, 102, 103, 104

class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", ctypes.c_void_p),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersionOrTimeout", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]

def _tray_icon_handle():
    """EXE에 들어 있는 아이콘을 쓰고, 없으면 Windows 기본 아이콘을 쓴다."""
    try:
        shell32 = ctypes.windll.shell32
        shell32.ExtractIconW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT]
        shell32.ExtractIconW.restype = ctypes.c_void_p
        h = shell32.ExtractIconW(None, str(Path(sys.executable)), 0)
        if h and h > 1:
            return h
    except Exception:
        pass
    u = ctypes.windll.user32
    u.LoadIconW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    u.LoadIconW.restype = ctypes.c_void_p
    return u.LoadIconW(None, wintypes.LPCWSTR(32512))  # IDI_APPLICATION

def _tray_data(hwnd, tip=None, flags=NIF_MESSAGE | NIF_ICON | NIF_TIP):
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = TRAY_UID
    nid.uFlags = flags
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = _tray_icon_handle()
    nid.szTip = (tip or APP_NAME)[:127]
    return nid

def _tray_add(hwnd, tip=None):
    try:
        shell32 = ctypes.windll.shell32
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        return bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(_tray_data(hwnd, tip))))
    except Exception:
        return False

def _tray_update(hwnd, tip):
    try:
        shell32 = ctypes.windll.shell32
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(_tray_data(hwnd, tip)))
    except Exception:
        pass

def _tray_remove(hwnd):
    try:
        shell32 = ctypes.windll.shell32
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = TRAY_UID
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    except Exception:
        pass

def _tray_tip():
    parts = ["출근 " + ("완료" if already_checked_in_today() else "미완료"),
             "퇴근 " + ("완료" if already_checked_out_today() else "미완료")]
    return f"{APP_NAME}\n오늘 " + " / ".join(parts)

def _tray_menu(hwnd):
    """트레이 아이콘 우클릭 메뉴. 고른 항목의 ID를 돌려준다 (안 고르면 0)."""
    u = ctypes.windll.user32
    u.CreatePopupMenu.restype = ctypes.c_void_p
    u.AppendMenuW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    u.TrackPopupMenu.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
    u.TrackPopupMenu.restype = ctypes.c_int
    u.DestroyMenu.argtypes = [ctypes.c_void_p]
    u.GetCursorPos.argtypes = [ctypes.c_void_p]
    u.SetForegroundWindow.argtypes = [ctypes.c_void_p]

    menu = u.CreatePopupMenu()
    if not menu:
        return 0
    try:
        MF_STRING, MF_SEPARATOR = 0x0, 0x800
        u.AppendMenuW(menu, MF_STRING, MENU_CHECKIN, "지금 출근 체크")
        u.AppendMenuW(menu, MF_STRING, MENU_CHECKOUT, "지금 퇴근 체크")
        u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        u.AppendMenuW(menu, MF_STRING, MENU_OPEN, "상태 창 열기")
        u.AppendMenuW(menu, MF_STRING, MENU_QUIT, "종료(자동 출퇴근 중지)")
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        # 메뉴를 띄우기 전에 포그라운드로 올려야 바깥을 클릭했을 때 메뉴가 닫힌다 (Win32 관례)
        u.SetForegroundWindow(hwnd)
        TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080
        return u.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                pt.x, pt.y, 0, hwnd, None)
    finally:
        u.DestroyMenu(menu)

def _confirm_tray_quit():
    return ctypes.windll.user32.MessageBoxW(
        None,
        "자동 출퇴근 체크를 중지할까요?\n\n"
        "중지하면 출근 자동 체크와 종료 시 퇴근 확인이 동작하지 않습니다.\n"
        "(다음에 Windows에 다시 로그인하면 자동으로 켜집니다)",
        APP_NAME, MB_YESNO | MB_ICONQUESTION) == IDYES

def _tray_action(mode, hwnd):
    """트레이 메뉴에서 고른 출근/퇴근을 백그라운드 스레드로 실행한다.
    메시지 루프를 막으면 종료 질의에 응답 못 하므로 절대 여기서 기다리지 않는다."""
    def work():
        cfg = load_config()
        ok, info = run_attend(mode, headless=not cfg.get("debug_show_browser", False), interactive=True)
        try:
            _tray_update(hwnd, _tray_tip())
        except Exception:
            pass
        label = "출근" if mode == "checkin" else "퇴근"
        _msgbox_timeout(f"{label} 체크 결과\n\n{info}", APP_NAME,
                        0x40 if ok else MB_ICONWARNING, timeout_ms=15000)
    threading.Thread(target=work, daemon=True).start()

def _open_status_window():
    """상태 창은 별도 프로세스로 띄운다. 감시기 프로세스 안에서 Tk를 돌리면
    메시지 루프가 엉켜서 종료 감시가 멈출 수 있다."""
    try:
        if _is_frozen():
            subprocess.Popen([sys.executable, "status"], creationflags=0x00000008, close_fds=True)
        else:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "status"],
                             creationflags=0x00000008, close_fds=True)
    except Exception:
        pass


def _shutdown_decision(cfg):
    """종료 시점에 바로 꺼도 되는지, 퇴근 체크를 물어야 하는지(ASK) 판단.
    Win32 API에 손대지 않는 순수 로직이라 가짜 설정값으로 그대로 테스트할 수 있다.
    시간 문턱은 두지 않는다 - 조퇴든 뭐든 퇴근 체크가 안 된 채로 끄려 하면 몇 시든 물어봐야
    누락이 조용히 생기지 않는다.
    OFF_NETWORK만 영구 허용(래치): 사내망이 아닌 게 확인되면 이번 세션 내내 다시 묻지 않는다.
    NET_UNKNOWN(조회 실패)/ALREADY_DONE은 래치하지 않는다 — 인터넷이 잠깐 끊긴 것뿐일 수 있고,
    퇴근 여부도 그때그때 바뀌는데 래치해버리면 그날 남은 시간 동안 확인이 영영 안 뜬다."""
    net = check_network(cfg, quick=True)
    if net == "no":
        return "OFF_NETWORK"
    if net == "unknown":
        return "NET_UNKNOWN"
    if already_checked_out_today():
        return "ALREADY_DONE"
    return "ASK"


def _set_block_reason(hwnd, text):
    """Windows 종료 화면('앱이 종료를 막고 있습니다')에 보일 안내 문구를 등록(text)/해제(None)한다.
    종료 화면이 앱이 띄운 팝업을 가릴 수 있어서, 사용자가 무엇을 해야 하는지 그 화면에서 알려준다."""
    try:
        u = ctypes.windll.user32
        u.ShutdownBlockReasonCreate.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        u.ShutdownBlockReasonDestroy.argtypes = [ctypes.c_void_p]
        if text:
            u.ShutdownBlockReasonCreate(hwnd, text)
        else:
            u.ShutdownBlockReasonDestroy(hwnd)
    except Exception:
        pass

BOOT_NET_WAIT_SEC = 300     # 부팅 직후엔 Wi-Fi/랜이 아직 안 붙었을 수 있어 최대 5분까지 기다린다
BOOT_NET_POLL_SEC = 10
BOOT_CHECKIN_TRIES = 3
BOOT_RETRY_GAP_SEC = 20

DAILY_POLL_SEC = 600

def _daily_checkin_loop(sleep=time.sleep, once=False):
    """로그온 직후 한 번 출근 체크하고, 그 뒤로도 날짜가 바뀌면 다시 체크한다.
    PC를 끄지 않고 계속 켜두는 사람은 재부팅이 없어서, 이 루프가 없으면 다음날 출근 체크가
    영영 실행되지 않는다."""
    while True:
        try:
            _boot_checkin_worker(sleep=sleep)
        except Exception:
            pass
        try:
            _maybe_auto_update()
        except Exception:
            pass
        if once:
            return
        sleep(DAILY_POLL_SEC)

def _boot_checkin_worker(sleep=time.sleep):
    """자동 출근 체크 1회분. 사내망일 때만, 오늘 아직 안 했을 때만 실행한다.
    메시지 루프(종료 감시)를 막지 않도록 별도 스레드에서 돌리는 것을 전제로 한다."""
    cfg = load_config()
    if not cfg.get("office_public_ip") or already_checked_in_today() or checkin_already_asked_today():
        return
    ip = get_public_ip(timeout=4)
    waited = 0
    while ip is None and waited < BOOT_NET_WAIT_SEC:
        sleep(BOOT_NET_POLL_SEC); waited += BOOT_NET_POLL_SEC
        ip = get_public_ip(timeout=4)
    if ip is None or ip != cfg["office_public_ip"]:
        return  # 인터넷이 없거나 사내망이 아님: 조용히 넘어간다
    info = ""
    for attempt in range(BOOT_CHECKIN_TRIES):
        if already_checked_in_today():
            return
        ok, info = run_attend("checkin", headless=not cfg.get("debug_show_browser", False), interactive=True)
        if ok:
            return
        if "로그인" in info or "어제 퇴근 체크가 빠졌을 수 있으니" in info:
            break  # 로그인 문제/출근 모호 상태(이미 물어봤음)는 재시도해도 같은 결과다
        if attempt < BOOT_CHECKIN_TRIES - 1:
            sleep(BOOT_RETRY_GAP_SEC)
    _notify_checkin_failed(info)


# ---------- 관리자 PIN ----------

def pin_hash(pin, salt):
    return hashlib.sha256((salt + pin).encode()).hexdigest()

def set_admin_pin(cfg, pin):
    salt = secrets.token_hex(8)
    cfg["admin_pin_salt"] = salt
    cfg["admin_pin_hash"] = pin_hash(pin, salt)
    save_config(cfg)

def verify_admin_pin(cfg, pin):
    salt = cfg.get("admin_pin_salt") or ""
    return bool(cfg.get("admin_pin_hash")) and pin_hash(pin, salt) == cfg.get("admin_pin_hash")


# ---------- Windows 종료 감시 ----------
# 정책: 사내망이 아니거나 이미 퇴근 처리됐으면 묻지 않고 바로 종료.
# 사내망 + 아직 미퇴근이면 물어보되, 예/아니오/체크 실패 어느 경우든 종료 자체는 항상 진행한다
# (자동화 실패로 인해 직원이 퇴근을 못 하게 막지 않는다).
def watch_shutdown():
    if os.name != "nt":
        print("Windows에서만 사용할 수 있습니다."); return

    from ctypes import wintypes

    # use_last_error=True 로 열어야 GetLastError 값을 안전하게 읽을 수 있다
    # (kernel32.GetLastError()를 따로 호출하면 그 사이 다른 호출이 값을 덮어쓸 수 있다).
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    # Local\ = 로그온 세션 단위. Global\은 같은 PC의 다른 사용자 세션의 감시기까지 막아버린다.
    mutex = kernel32.CreateMutexW(None, False, WATCHER_MUTEX_NAME)
    if not mutex or ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: 이미 실행 중
        return

    user32 = ctypes.windll.user32
    # 64비트에서 핸들/포인터는 8바이트다. argtypes/restype를 지정하지 않으면 ctypes가 이를
    # 32비트로 잘라내려다 OverflowError를 내거나 잘못된 값을 넘긴다.
    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.PostQuitMessage.argtypes = [ctypes.c_int]

    WM_QUERYENDSESSION = 0x0011
    WM_ENDSESSION = 0x0016
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    ENDSESSION_LOGOFF = 0x80000000

    # LRESULT는 64비트에서 8바이트다 (c_long으로 두면 잘린다).
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
        ctypes.c_size_t, ctypes.c_size_t
    )

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", ctypes.c_void_p),
            ("hIcon", ctypes.c_void_p),
            ("hCursor", ctypes.c_void_p),
            ("hbrBackground", ctypes.c_void_p),
            ("lpszMenuName", ctypes.c_wchar_p),
            ("lpszClassName", ctypes.c_wchar_p),
        ]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", ctypes.c_uint),
            ("wParam", ctypes.c_size_t),
            ("lParam", ctypes.c_size_t),
            ("time", ctypes.c_uint),
            ("pt_x", ctypes.c_long),
            ("pt_y", ctypes.c_long),
        ]

    allow = {"value": False}
    busy = {"value": False}

    def on_query_end_session(hwnd, lparam):
        if allow["value"]:
            return 1
        if busy["value"]:
            return 0

        # 로그오프/사용자 전환은 '퇴근'이 아니므로 그냥 통과시킨다.
        if lparam & ENDSESSION_LOGOFF:
            return 1

        # 네트워크를 보기 전에 먼저 안내 문구를 걸어둔다 (조회에 몇 초 걸릴 수 있다).
        _set_block_reason(hwnd, "퇴근 체크 여부를 확인 중입니다. 잠시만 기다려 주세요.")
        try:
            cfg = load_config()
            decision = _shutdown_decision(cfg)

            if decision == "OFF_NETWORK":
                allow["value"] = True   # 사내망이 아닌 게 확인됨 -> 이번 세션엔 다시 묻지 않는다
                return 1
            if decision in ("NET_UNKNOWN", "ALREADY_DONE"):
                return 1

            # 퇴근 미체크 상태. 정책: "체크될 때까지 매번 다시 물어본다" - 한 번 거절했다고
            # 다음 종료 시도부터 묻지 않으면, 결국 퇴근 체크 없이 넘어가는 날이 생긴다.
            busy["value"] = True
            _set_block_reason(hwnd, "퇴근 체크가 아직 안 되어 있습니다. 화면이 멈춘 것처럼 보이면 [취소]를 누르고 안내창에서 선택해 주세요.")
            answer = _confirm_checkout_dialog()

            if answer == "away":
                # 자리에 사람이 없다 -> 이번 종료는 막지 않는다 (예약 재부팅이 밤새 취소되면 안 된다).
                # allow를 영구히 세우지는 않는다 - 다음에 또 종료를 시도하면 그때도 다시 물어본다.
                busy["value"] = False
                return 1

            if answer == "yes":
                _set_block_reason(hwnd, "하이웍스 퇴근 체크 중입니다. 끝나면 자동으로 종료됩니다. 잠시만 기다려 주세요.")
                ok, info = run_attend("checkout", headless=not cfg.get("debug_show_browser", False))
                if ok:
                    _notify_checkout_success(info)
                else:
                    _notify_checkout_failed(info)
                # 이 종료 요청은 우리가 붙잡아 취소된 상태이므로, 사용자가 고른 대로 우리가 다시 건다.
                allow["value"] = True
                busy["value"] = False
                shutdown_now(delay=int(cfg.get("shutdown_delay", 2)))
                return 0

            # "아니오": 이번 종료는 취소하고 퇴근 체크를 하도록 유도한다.
            # 다음에 다시 종료를 눌러도 퇴근 체크가 안 되어 있으면 또 물어본다.
            busy["value"] = False
            return 0
        finally:
            _set_block_reason(hwnd, None)

    def proc(hwnd, msg, wparam, lparam):
        if msg == WM_QUERYENDSESSION:
            # 여기서 예외가 나면 ctypes가 그걸 삼키고 0(=종료 거부)을 돌려준다. 그러면 직원이
            # 컴퓨터를 못 끄게 되므로, 무슨 일이 있어도 종료를 허용하는 쪽으로 빠져나간다.
            try:
                return on_query_end_session(hwnd, lparam)
            except Exception:
                allow["value"] = True
                busy["value"] = False
                try: _set_block_reason(hwnd, None)
                except Exception: pass
                return 1

        if msg == WM_ENDSESSION:
            return 0

        if msg == WM_TRAY:
            # lparam 하위 16비트가 실제 마우스 이벤트
            ev = lparam & 0xFFFF
            WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
            try:
                if ev in (WM_RBUTTONUP, WM_LBUTTONUP):
                    if ev == WM_LBUTTONUP:
                        _tray_update(hwnd, _tray_tip())
                    choice = _tray_menu(hwnd)
                    if choice == MENU_CHECKIN:
                        _tray_action("checkin", hwnd)
                    elif choice == MENU_CHECKOUT:
                        _tray_action("checkout", hwnd)
                    elif choice == MENU_OPEN:
                        _open_status_window()
                    elif choice == MENU_QUIT:
                        if _confirm_tray_quit():
                            _tray_remove(hwnd)
                            user32.PostQuitMessage(0)
                elif ev == WM_LBUTTONDBLCLK:
                    _open_status_window()
            except Exception:
                pass
            return 0

        if msg in (WM_CLOSE, WM_DESTROY):
            _tray_remove(hwnd)
            user32.PostQuitMessage(0)
            return 0

        try:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except Exception:
            return 0

    cb = WNDPROC(proc)
    inst = kernel32.GetModuleHandleW(None)
    cls = WATCHER_CLASS

    wc = WNDCLASS()
    wc.lpfnWndProc = cb
    wc.hInstance = inst
    wc.lpszClassName = cls

    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(
        0, cls, APP_NAME, 0,      # dwExStyle, 클래스, 창 제목, dwStyle
        0, 0, 0, 0,               # x, y, 너비, 높이 (화면에 안 보이는 창)
        None, None, inst, None    # 부모, 메뉴, hInstance, lpParam
    )

    _tray_add(hwnd, _tray_tip())

    # 부팅 시 자동 출근 체크: 성공하면 알림 없이 넘어가고(미니멀 UI), 끝내 실패했을 때만 알린다.
    # 별도 스레드로 돌려서 메시지 루프가 바로 시작되게 한다 (종료 질의에 즉시 답해야 하므로).
    threading.Thread(target=_daily_checkin_loop, daemon=True).start()

    msg = MSG()
    while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# ---------- 화면 공통 ----------
# clam 테마를 쓰는 이유: vista(윈도우 기본) 테마는 OS가 그려서 배경/버튼 색을
# ttk.Style로 바꿔도 대부분 무시한다. clam은 순수 tk 렌더링이라 색을 완전히 입힐 수 있다.

def _apply_style(root):
    root.configure(bg=COLOR_BG)
    style = ttk.Style(root)
    try: style.theme_use("clam")
    except: pass

    style.configure(".", font=("Malgun Gothic", 10))
    style.configure("Card.TFrame", background=COLOR_CARD)
    style.configure("TFrame", background=COLOR_CARD)
    style.configure("TLabel", background=COLOR_CARD, foreground=COLOR_TEXT)
    style.configure("Header.TLabel", font=("Malgun Gothic", 16, "bold"), background=COLOR_CARD, foreground=COLOR_TEXT)
    style.configure("Sub.TLabel", font=("Malgun Gothic", 10), background=COLOR_CARD, foreground=COLOR_SUBTEXT)
    style.configure("Hint.TLabel", font=("Malgun Gothic", 8), background=COLOR_CARD, foreground=COLOR_HINT)
    style.configure("Status.TLabel", font=("Malgun Gothic", 11), background=COLOR_CARD, foreground=COLOR_TEXT)
    style.configure("Field.TLabel", font=("Malgun Gothic", 9), background=COLOR_CARD, foreground=COLOR_SUBTEXT)

    style.configure("TEntry", padding=8, relief="flat",
                     fieldbackground="#FFFFFF", bordercolor=COLOR_BORDER,
                     lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER)
    style.configure("TSpinbox", padding=6, relief="flat", fieldbackground="#FFFFFF",
                     bordercolor=COLOR_BORDER, arrowsize=14)
    style.configure("TCheckbutton", background=COLOR_CARD, foreground=COLOR_TEXT)

    style.configure("Accent.TButton", font=("Malgun Gothic", 11, "bold"), foreground="#FFFFFF",
                     background=COLOR_ACCENT, borderwidth=0, padding=(14, 10))
    style.map("Accent.TButton", background=[("active", COLOR_ACCENT_DARK), ("pressed", COLOR_ACCENT_DARK)])

    style.configure("Secondary.TButton", font=("Malgun Gothic", 10), foreground=COLOR_TEXT,
                     background="#F3F4F6", borderwidth=1, bordercolor=COLOR_BORDER, padding=(12, 9))
    style.map("Secondary.TButton", background=[("active", "#E5E7EB")])

    return style

def _card(root, pad=26):
    """흰 카드/회색 배경 대비 없이, 창 배경 위에 여백만 두고 내용을 배치한다."""
    frm = ttk.Frame(root, padding=pad, style="Card.TFrame")
    frm.pack(fill="both", expand=True)
    return frm

def _resource_path(rel):
    """동봉 리소스 경로. 소스로 실행 중이면 이 파일 옆, 빌드된 exe면 PyInstaller가 풀어둔 임시 폴더 기준."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel

LOGO_PATH = _resource_path("assets/logo.png")

def _header(frm, title=APP_NAME, subtitle=None):
    """위에 회사 로고, 아래에 제목/부제."""
    try:
        logo = tk.PhotoImage(file=str(LOGO_PATH)).subsample(3, 3)
        logo_lbl = ttk.Label(frm, image=logo)
        logo_lbl.image = logo  # 참조를 안 들고 있으면 가비지 컬렉션으로 이미지가 사라진다
        logo_lbl.pack(anchor="w", pady=(0, 10))
    except Exception:
        pass  # 로고가 없어도 화면은 떠야 한다
    ttk.Label(frm, text=title, style="Header.TLabel").pack(anchor="w")
    if subtitle:
        ttk.Label(frm, text=subtitle, style="Sub.TLabel").pack(anchor="w", pady=(4, 0))

def _credit_label(frm, on_secret_click=None):
    lbl = ttk.Label(frm, text=CREDIT, style="Hint.TLabel",
                     cursor="hand2" if on_secret_click else "")
    lbl.pack(anchor="e", pady=(18, 0))
    if on_secret_click:
        state = {"count": 0, "last": 0.0}
        def _click(event=None):
            now = time.time()
            if now - state["last"] > 1.5:
                state["count"] = 0
            state["last"] = now
            state["count"] += 1
            if state["count"] >= 5:
                state["count"] = 0
                on_secret_click()
        lbl.bind("<Button-1>", _click)
    return lbl


def _run_in_thread(root, work, done):
    """오래 걸리는 작업(브라우저 자동화)을 스레드에서 돌리고, 끝나면 UI 스레드에서 done(결과)을 부른다.
    Tk 창이 작업 중 '응답 없음'으로 굳지 않게 한다."""
    box = {}
    def target():
        try:
            box["r"] = work()
        except Exception as e:
            box["r"] = (False, f"오류: {e}")
    t = threading.Thread(target=target, daemon=True)
    t.start()
    def poll():
        if t.is_alive():
            root.after(150, poll)
        else:
            done(box["r"])
    poll()


# ---------- 메인 화면 (로그인 등록 + 오늘 상태 + 수동 버튼) ----------

def _network_text():
    cfg = load_config()
    if not cfg.get("office_public_ip"):
        return "회사망이 아직 등록되지 않았습니다 (관리자 설정에서 등록)"
    net = check_network(cfg)
    if net == "yes":
        return "현재 사내망입니다 — 자동 출퇴근이 동작합니다"
    if net == "unknown":
        return "네트워크를 확인할 수 없습니다 (인터넷 연결 확인)"
    return "사내망이 아니라서 자동 출퇴근이 동작하지 않습니다"

def _status_pill(parent, label, done):
    row = ttk.Frame(parent, style="Card.TFrame"); row.pack(fill="x", pady=3)
    ttk.Label(row, text=label, style="Status.TLabel").pack(side="left")
    color = COLOR_SUCCESS if done else COLOR_HINT
    text = "완료" if done else "미완료"
    tk.Label(row, text=text, bg=color, fg="#FFFFFF", font=("Malgun Gothic", 9, "bold"),
             padx=10, pady=2).pack(side="right")

AUTO_MINIMIZE_MS = 2500

def main_window(auto_minimize=True):
    """한 화면에서 전부 처리한다.
      - 아이디/비밀번호가 저장돼 있으면: '로그인이 되어 있네요 :)' 를 보여주고 잠시 뒤 최소화
      - 저장돼 있지 않으면: 등록 폼을 띄운다 (한 번 등록하면 계속 저장된다)
      - 어느 쪽이든 화면 하단에 수동 출근/퇴근 버튼이 있다"""
    root = tk.Tk(); root.title(APP_NAME); root.resizable(False, False)
    try: root.iconname(APP_NAME)
    except: pass
    _apply_style(root)

    logged_in = has_credentials()
    win_width = 430 if logged_in else 470
    root.geometry(f"{win_width}x200")  # 높이는 내용을 다 채운 뒤 실제 필요한 만큼으로 다시 잡는다(아래)

    frm = _card(root)
    state = {"minimize_job": None}

    def cancel_auto_minimize(event=None):
        # 사용자가 창을 만지기 시작하면 자동 최소화를 취소한다 (쓰던 창이 갑자기 내려가면 안 된다)
        if state["minimize_job"] is not None:
            try: root.after_cancel(state["minimize_job"])
            except Exception: pass
            state["minimize_job"] = None
    for ev in ("<Button-1>", "<Key>", "<MouseWheel>"):
        root.bind_all(ev, cancel_auto_minimize, add="+")

    # ---- 상단: 로그인 상태 또는 등록 폼 ----
    if logged_in:
        _header(frm, subtitle="로그인이 되어 있네요 :)")
        saved_id, _ = load_credentials()
        ttk.Label(frm, text=f"저장된 아이디: {saved_id}", style="Hint.TLabel").pack(anchor="w", pady=(6, 0))
    else:
        _header(frm, subtitle="처음 한 번만 로그인 정보를 입력하면 됩니다.")
        ttk.Frame(frm, style="Card.TFrame", height=18).pack()
        ttk.Label(frm, text="하이웍스 아이디 (예: 90807867 — @intriholdings.com은 안 써도 됩니다)",
                  style="Field.TLabel").pack(anchor="w")
        user = ttk.Entry(frm); user.pack(fill="x", pady=(4, 12)); user.focus()
        ttk.Label(frm, text="비밀번호", style="Field.TLabel").pack(anchor="w")
        pw = ttk.Entry(frm, show="●"); pw.pack(fill="x", pady=(4, 8))
        show = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="비밀번호 표시", variable=show,
                        command=lambda: pw.configure(show="" if show.get() else "●")).pack(anchor="w", pady=(0, 14))

    ttk.Frame(frm, style="Card.TFrame", height=14).pack()

    # ---- 가운데: 오늘 상태 + 네트워크 ----
    status_holder = ttk.Frame(frm, style="Card.TFrame"); status_holder.pack(fill="x")
    def redraw_status():
        for w in status_holder.winfo_children(): w.destroy()
        _status_pill(status_holder, "오늘 출근", already_checked_in_today())
        _status_pill(status_holder, "오늘 퇴근", already_checked_out_today())
    redraw_status()

    net_lbl = ttk.Label(frm, text="네트워크 확인 중...", style="Hint.TLabel", wraplength=360)
    net_lbl.pack(anchor="w", pady=(10, 0))
    _run_in_thread(root, lambda: (True, _network_text()), lambda r: net_lbl.configure(text=r[1]))

    result_lbl = ttk.Label(frm, text="", style="Sub.TLabel", wraplength=360)
    result_lbl.pack(anchor="w", pady=(12, 10))

    buttons = []
    def set_busy(busy):
        for b in buttons:
            b.configure(state="disabled" if busy else "normal")

    def manual(mode):
        cancel_auto_minimize()
        if not has_credentials():
            messagebox.showwarning("등록 필요", "먼저 하이웍스 아이디/비밀번호를 등록해 주세요."); return
        set_busy(True)
        result_lbl.configure(text="처리 중입니다... (브라우저가 백그라운드에서 동작합니다)")
        cfg = load_config()
        def done(r):
            result_lbl.configure(text=r[1]); redraw_status(); set_busy(False)
        _run_in_thread(root, lambda: run_attend(mode, headless=not cfg.get("debug_show_browser", False), interactive=True), done)

    checkin_var = tk.BooleanVar(value=True)
    checkout_var = tk.BooleanVar(value=True)

    def manual_selected():
        cancel_auto_minimize()
        if not has_credentials():
            messagebox.showwarning("등록 필요", "먼저 하이웍스 아이디/비밀번호를 등록해 주세요."); return
        modes = [m for m, var in (("checkin", checkin_var), ("checkout", checkout_var)) if var.get()]
        if not modes:
            messagebox.showwarning("선택 필요", "출근 체크 또는 퇴근 체크를 하나 이상 선택해 주세요."); return
        set_busy(True)
        result_lbl.configure(text="처리 중입니다... (브라우저가 백그라운드에서 동작합니다)")
        cfg = load_config()
        def work():
            lines = []
            overall_ok = True
            for m in modes:
                ok, msg = run_attend(m, headless=not cfg.get("debug_show_browser", False), interactive=True)
                lines.append(f"[{'출근' if m == 'checkin' else '퇴근'}] {msg}")
                if not ok:
                    overall_ok = False
                    break  # 앞 단계가 실패하면 이어서 할 의미가 없다 (출근 안 된 채 퇴근 시도 등)
            return overall_ok, "\n".join(lines)
        def done(r):
            result_lbl.configure(text=r[1]); redraw_status(); set_busy(False)
        _run_in_thread(root, work, done)

    # ---- 등록 버튼 (미등록 상태에서만) ----
    if not logged_in:
        def finish(r):
            ok, msg = r
            set_busy(False)
            result_lbl.configure(text=msg)
            if not ok:
                messagebox.showerror("로그인 실패", msg); return
            cfg = load_config()
            first_time = not cfg.get("setup_complete")
            cfg["setup_complete"] = True
            ip = get_public_ip() if not cfg.get("office_public_ip") else None
            if ip:
                cfg["office_public_ip"] = ip
            save_config(cfg)
            net_note = ("현재 네트워크를 회사망으로 등록했습니다." if ip else
                        "공인 IP 확인에 실패해 회사망이 등록되지 않았습니다. 관리자 설정에서 등록해 주세요.") if first_time else \
                       "기존 회사망 설정은 그대로 유지됩니다."
            messagebox.showinfo("등록 완료",
                                "로그인 정보가 저장되었습니다.\n\n"
                                f"{net_note}\n"
                                "이제 Windows에 로그인하면 자동으로 출근 체크하고,\n"
                                "오후 6시 이후 PC를 종료하면 퇴근 여부를 물어봅니다.")
            root.destroy()
            main_window(auto_minimize=True)   # 로그인된 화면으로 새로 연다

        def do_register():
            cancel_auto_minimize()
            u = user.get().strip(); p = pw.get()
            if not u or not p:
                messagebox.showwarning("입력 필요", "아이디와 비밀번호를 입력해 주세요."); return
            set_busy(True)
            result_lbl.configure(text="하이웍스 로그인 및 출근 체크 중... (브라우저 창이 잠깐 열립니다)")
            def work():
                ok, msg = run_attend("checkin", u, p, headless=False, interactive=True)
                if ok:
                    save_credentials(u, p)   # 로그인이 실제로 성공했을 때만 저장한다
                return ok, msg
            _run_in_thread(root, work, finish)

        register_btn = ttk.Button(frm, text="로그인하고 자동화 설정하기", command=do_register, style="Accent.TButton")
        register_btn.pack(fill="x", pady=(0, 10))
        buttons.append(register_btn)
        ttk.Label(frm, text="※ 비밀번호는 이 PC의 Windows 계정으로만 풀 수 있게 암호화되어 저장됩니다.",
                  style="Hint.TLabel", wraplength=360).pack(anchor="w", pady=(0, 6))

    # ---- 하단: 수동 출근/퇴근 ----
    bottom = ttk.Frame(frm, style="Card.TFrame")
    bottom.pack(side="bottom", fill="x", pady=(10, 0))
    _credit_label(bottom, on_secret_click=lambda: (cancel_auto_minimize(), open_admin(root)))
    row = ttk.Frame(bottom, style="Card.TFrame"); row.pack(side="bottom", fill="x")
    bi = ttk.Button(row, text="지금 출근 체크", command=lambda: manual("checkin"), style="Accent.TButton")
    bo = ttk.Button(row, text="지금 퇴근 체크", command=lambda: manual("checkout"), style="Secondary.TButton")
    bi.pack(side="left", expand=True, fill="x", padx=(0, 4))
    bo.pack(side="left", expand=True, fill="x", padx=(4, 0))
    buttons.extend([bi, bo])

    sel_row = ttk.Frame(bottom, style="Card.TFrame"); sel_row.pack(side="bottom", fill="x", pady=(0, 6))
    cb_in = ttk.Checkbutton(sel_row, text="출근 체크", variable=checkin_var)
    cb_out = ttk.Checkbutton(sel_row, text="퇴근 체크", variable=checkout_var)
    run_sel_btn = ttk.Button(sel_row, text="선택 실행", command=manual_selected, style="Secondary.TButton")
    cb_in.pack(side="left")
    cb_out.pack(side="left", padx=(10, 0))
    run_sel_btn.pack(side="right")
    buttons.extend([cb_in, cb_out, run_sel_btn])

    if logged_in and auto_minimize:
        state["minimize_job"] = root.after(AUTO_MINIMIZE_MS, root.iconify)

    # 고정 높이를 미리 추측해서 넣으면 내용이 조금만 늘어도 잘려서 안 보이게 된다
    # (실제로 한 번 겪었다) - 다 채운 뒤 실제 필요한 높이를 재서 맞춘다.
    root.update_idletasks()
    root.geometry(f"{win_width}x{frm.winfo_reqheight() + 16}")

    root.mainloop()

def setup_gui():
    main_window(auto_minimize=False)

def status_window():
    main_window()


# ---------- 관리자 패널 ----------

def open_admin(parent):
    cfg = load_config()
    if not cfg.get("admin_pin_hash"):
        p1 = simpledialog.askstring(f"{APP_NAME} - 관리자 PIN 설정",
                                     "처음 접속입니다. 사용할 관리자 PIN을 입력하세요 (4자리 이상):",
                                     show="*", parent=parent)
        if not p1 or len(p1) < 4:
            return
        p2 = simpledialog.askstring(f"{APP_NAME} - 관리자 PIN 설정",
                                     "확인을 위해 PIN을 한 번 더 입력하세요:", show="*", parent=parent)
        if p1 != p2:
            messagebox.showerror("실패", "PIN이 일치하지 않습니다.", parent=parent); return
        set_admin_pin(cfg, p1)
        messagebox.showinfo("설정 완료", "관리자 PIN이 설정되었습니다.", parent=parent)
    else:
        pin = simpledialog.askstring(f"{APP_NAME} - 관리자 인증", "관리자 PIN을 입력하세요:",
                                      show="*", parent=parent)
        if pin is None:
            return
        if not verify_admin_pin(cfg, pin):
            messagebox.showerror("실패", "PIN이 올바르지 않습니다.", parent=parent); return

    admin_panel(parent)

def admin_panel(parent):
    cfg = load_config()
    win = tk.Toplevel(parent); win.title(f"{APP_NAME} - 관리자 설정")
    win.geometry("420x600"); win.resizable(False, False)
    win.configure(bg=COLOR_BG)
    frm = _card(win, pad=24)

    ttk.Label(frm, text="관리자 설정", style="Header.TLabel").pack(anchor="w")
    ttk.Frame(frm, style="Card.TFrame", height=16).pack()

    ttk.Label(frm, text="회사망 공인 IP", style="Field.TLabel").pack(anchor="w")
    ip_val = ttk.Label(frm, text=cfg.get("office_public_ip") or "(미설정)", style="Status.TLabel")
    ip_val.pack(anchor="w", pady=(2, 8))
    ip_status = ttk.Label(frm, text="", style="Sub.TLabel", wraplength=330)
    def reset_ip():
        ip_status.configure(text="확인 중..."); win.update()
        ip = get_public_ip()
        if ip:
            c = load_config(); c["office_public_ip"] = ip; save_config(c)
            ip_val.configure(text=ip)
            ip_status.configure(text="현재 네트워크로 재등록했습니다.")
        else:
            ip_status.configure(text="IP 확인 실패 (인터넷 연결을 확인하세요).")
    ttk.Button(frm, text="지금 이 네트워크를 회사망으로 등록", command=reset_ip, style="Secondary.TButton").pack(anchor="w", fill="x")
    ip_status.pack(anchor="w", pady=(6, 18))

    debug_var = tk.BooleanVar(value=cfg.get("debug_show_browser", False))
    ttk.Checkbutton(frm, text="자동화 실행 시 브라우저 창 표시 (디버그용)", variable=debug_var).pack(anchor="w", pady=(0, 18))

    status_holder = ttk.Frame(frm, style="Card.TFrame"); status_holder.pack(fill="x")
    def redraw():
        for w in status_holder.winfo_children(): w.destroy()
        ttk.Label(status_holder, text="오늘 상태", style="Field.TLabel").pack(anchor="w")
        _status_pill(status_holder, "출근", already_checked_in_today())
        _status_pill(status_holder, "퇴근", already_checked_out_today())
    redraw()

    def reset_today():
        if messagebox.askyesno("확인", "오늘 출퇴근 기록을 초기화할까요? (테스트용)", parent=win):
            save_state({}); redraw()
    ttk.Button(frm, text="오늘 기록 초기화", command=reset_today, style="Secondary.TButton").pack(anchor="w", fill="x", pady=(10, 18))

    def change_pin():
        p1 = simpledialog.askstring(f"{APP_NAME} - PIN 변경", "새 PIN을 입력하세요 (4자리 이상):", show="*", parent=win)
        if not p1 or len(p1) < 4:
            return
        p2 = simpledialog.askstring(f"{APP_NAME} - PIN 변경", "확인을 위해 한 번 더 입력하세요:", show="*", parent=win)
        if p1 != p2:
            messagebox.showerror("실패", "PIN이 일치하지 않습니다.", parent=win); return
        # 새 PIN이 확정된 뒤에만 교체한다
        set_admin_pin(load_config(), p1)
        messagebox.showinfo("변경 완료", "관리자 PIN이 변경되었습니다.", parent=win)
    ttk.Button(frm, text="관리자 PIN 변경", command=change_pin, style="Secondary.TButton").pack(anchor="w", fill="x")

    saved_id = load_credentials()[0]
    ttk.Label(frm, text=f"저장된 로그인: {saved_id or '(없음)'}", style="Field.TLabel").pack(anchor="w", pady=(16, 4))
    def forget_login():
        if messagebox.askyesno("확인", "저장된 하이웍스 아이디/비밀번호와 로그인 세션을 삭제할까요?\n"
                                       "다음 실행 때 다시 등록해야 자동 출퇴근이 동작합니다.", parent=win):
            clear_credentials()
            try: SESSION_FILE.unlink(missing_ok=True)
            except Exception: pass
            c = load_config(); c["setup_complete"] = False; save_config(c)
            messagebox.showinfo("삭제 완료", "저장된 로그인 정보를 삭제했습니다.", parent=win)
            win.destroy()
    ttk.Button(frm, text="저장된 로그인 정보 삭제", command=forget_login, style="Secondary.TButton").pack(anchor="w", fill="x")

    def do_save():
        c = load_config()
        c["debug_show_browser"] = debug_var.get()
        save_config(c)
        win.destroy()
    ttk.Button(frm, text="저장하고 닫기", command=do_save, style="Accent.TButton").pack(fill="x", pady=(20, 0))


# ---------- 설치 / 제거 (EXE 하나가 설치 프로그램을 겸한다) ----------
# 배치/PowerShell 스크립트를 쓰지 않는다: 한글이 든 .cmd는 한국어 Windows(cp949)에서 깨지고,
# PowerShell은 실행정책·보안 솔루션에 막히거나 의심 행위로 잡히기 쉽다.

INSTALL_DIR = _app_root("LOCALAPPDATA") / "IntriHoldingsAttendance"
INSTALLED_EXE = INSTALL_DIR / f"{APP_NAME}.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "IntriHoldingsAttendance"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\IntriHoldingsAttendance"
WATCHER_CLASS = "IntriHoldingsAttendanceShutdownWatcherV3"

def _is_frozen():
    return bool(getattr(sys, "frozen", False))

def _running_from_install_dir():
    try:
        return Path(sys.executable).resolve().parent == INSTALL_DIR.resolve()
    except Exception:
        return False

def _msgbox(text, icon=0x40):  # 0x40 정보, 0x10 오류, 0x30 경고
    ctypes.windll.user32.MessageBoxW(0, text, APP_NAME, icon)

def _clean_relay_copies():
    """제거용으로 %TEMP%에 복사해 둔 사본을 정리한다 (48MB짜리가 계속 쌓이면 안 된다).
    지금 실행 중인 사본은 자신을 못 지우므로 다음 설치/제거 때 정리된다."""
    me = Path(sys.executable).resolve() if _is_frozen() else None
    for f in _app_root("TEMP").glob("intri_uninstall_*.exe"):
        try:
            if me is None or f.resolve() != me:
                f.unlink()
        except Exception:
            pass

def _ask_yesno(text):
    return ctypes.windll.user32.MessageBoxW(0, text, APP_NAME, MB_YESNO | MB_ICONQUESTION) == IDYES

def _stop_watcher(timeout=15.0):
    """실행 중인 종료 감시기를 창 메시지(WM_CLOSE)로 정상 종료시키고, 프로세스가 완전히
    끝날 때까지 기다린다. 창만 사라지고 프로세스가 남아 있으면 뮤텍스를 계속 쥐고 있어서
    새로 띄운 감시기가 조용히 죽는다."""
    u = ctypes.windll.user32
    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowW.restype = ctypes.c_void_p
    u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    k = ctypes.windll.kernel32
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = ctypes.c_void_p
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    k.CloseHandle.argtypes = [ctypes.c_void_p]

    hwnd = u.FindWindowW(WATCHER_CLASS, None)
    if not hwnd:
        return True
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = k.OpenProcess(0x00100000, False, pid.value) if pid.value else None  # SYNCHRONIZE
    u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    try:
        if handle:
            return k.WaitForSingleObject(handle, int(timeout * 1000)) == 0  # WAIT_OBJECT_0
    finally:
        if handle:
            k.CloseHandle(handle)
    end = time.time() + timeout
    while time.time() < end:
        if not u.FindWindowW(WATCHER_CLASS, None):
            return True
        time.sleep(0.2)
    return False

class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
    ]

def _find_pids_running(exe_path):
    """이 exe 파일을 실행 중인 모든 프로세스의 PID를 찾는다 (자기 자신은 제외).
    감시기뿐 아니라 상태창/설정창 등 - 무엇이 열려 있든 exe 파일을 잠그고 있으면
    설치/업데이트/제거가 실패하므로, 이름이 아니라 실제 실행 경로로 정확히 대조한다."""
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32)]
    k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32)]
    snap = k.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == -1:
        return []
    exe_name = exe_path.name.lower()
    candidates = []
    try:
        entry = _PROCESSENTRY32(); entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        if k.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                if entry.szExeFile.lower() == exe_name and entry.th32ProcessID != os.getpid():
                    candidates.append(entry.th32ProcessID)
                if not k.Process32NextW(snap, ctypes.byref(entry)):
                    break
    finally:
        k.CloseHandle(snap)

    # 이름만으로는 동명이인일 수 있으니(다른 폴더의 같은 파일명) 실제 경로까지 확인한다.
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = ctypes.c_void_p
    k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    verified = []
    for pid in candidates:
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                try:
                    if Path(buf.value).resolve() == exe_path.resolve():
                        verified.append(pid)
                except OSError:
                    pass
        finally:
            k.CloseHandle(h)
    return verified

def _stop_all_instances(exe_path, timeout=15.0):
    """이 exe의 모든 실행 중인 인스턴스를 정리한다 (감시기, 상태창, 설정창 등 무엇이든).
    설치/업데이트/제거 전에 파일 잠금을 풀기 위함 - 하나라도 열려 있으면 덮어쓰기/삭제가
    실패한다. 먼저 창이 있으면 곱게 닫아보고(WM_CLOSE), 시간 안에 안 끝나면 강제 종료한다."""
    _stop_watcher(timeout=timeout)  # 감시기는 퇴근 체크 중일 수 있으니 정상 종료 메시지로 우선 시도

    u = ctypes.windll.user32
    u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_ssize_t), ctypes.c_ssize_t]
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
    ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_ssize_t)

    def close_windows_of(pid):
        def cb(hwnd, _):
            wpid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid:
                u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return True
        u.EnumWindows(ENUM(cb), 0)

    deadline = time.time() + timeout
    while time.time() < deadline:
        pids = _find_pids_running(exe_path)
        if not pids:
            return True
        for pid in pids:
            close_windows_of(pid)
        time.sleep(0.3)

    # 곱게 안 닫히면 강제 종료 (설치를 진행하기로 한 이상 끝까지 막히면 안 된다)
    k = ctypes.windll.kernel32
    k.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
    for pid in _find_pids_running(exe_path):
        h = k.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if h:
            k.TerminateProcess(h, 0)
            k.CloseHandle(h)
    time.sleep(0.5)
    return not _find_pids_running(exe_path)

def _desktop_dir():
    """OneDrive 등으로 리디렉션된 바탕화면 위치까지 반영한 실제 경로."""
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(0, 0x0010, 0, 0, buf)  # CSIDL_DESKTOPDIRECTORY
    return Path(buf.value) if buf.value else Path.home() / "Desktop"

def _create_shortcut(lnk_path, target, args="", workdir=None, description=""):
    """바로가기(.lnk) 생성. 스크립트 호스트 없이 COM(IShellLinkW)을 직접 호출한다."""
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
    def guid(text):
        g = GUID()
        ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(g))
        return g
    def method(obj, index, *argtypes, restype=ctypes.HRESULT):
        vtbl = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])

    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)
    try:
        clsid_shelllink = guid("{00021401-0000-0000-C000-000000000046}")
        iid_shelllink = guid("{000214F9-0000-0000-C000-000000000046}")
        iid_persistfile = guid("{0000010B-0000-0000-C000-000000000046}")
        link = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(clsid_shelllink), None, 1, ctypes.byref(iid_shelllink), ctypes.byref(link))
        try:
            method(link, 20, wintypes.LPCWSTR)(link, str(target))                    # SetPath
            method(link, 9, wintypes.LPCWSTR)(link, str(workdir or Path(target).parent))  # SetWorkingDirectory
            if args:
                method(link, 11, wintypes.LPCWSTR)(link, args)                       # SetArguments
            if description:
                method(link, 7, wintypes.LPCWSTR)(link, description)                 # SetDescription
            persist = ctypes.c_void_p()
            method(link, 0, ctypes.c_void_p, ctypes.c_void_p)(link, ctypes.byref(iid_persistfile), ctypes.byref(persist))  # QueryInterface
            try:
                method(persist, 6, wintypes.LPCWSTR, wintypes.BOOL)(persist, str(lnk_path), True)  # IPersistFile::Save
            finally:
                method(persist, 2, restype=ctypes.c_ulong)(persist)                  # Release
        finally:
            method(link, 2, restype=ctypes.c_ulong)(link)                            # Release
    finally:
        ole32.CoUninitialize()

# ---------- 깃헙 릴리스 자동 업데이트 ----------

def _version_tuple(v):
    """'v1.3.0' 같은 깃헙 태그와 '1.2.0' 내부 버전을 섞어 비교할 수 있게 숫자 튜플로 바꾼다."""
    parts = []
    for p in v.lstrip("vV").split("."):
        try: parts.append(int(p))
        except ValueError: parts.append(0)
    return tuple(parts)

def check_for_update(timeout=6):
    """깃헙 최신 릴리스를 조회한다. 더 새 버전이 있으면 (True, exe 다운로드 URL, 태그), 아니면 (False, None, None)."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": UPDATE_USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rel = json.loads(r.read().decode())
        if _version_tuple(rel.get("tag_name", "")) <= _version_tuple(APP_VERSION):
            return False, None, None
        for asset in rel.get("assets", []):
            if asset.get("name", "").endswith(".exe"):
                return True, asset.get("browser_download_url"), rel.get("tag_name")
        return False, None, None
    except Exception:
        return False, None, None

def apply_update(download_url, timeout=120):
    """새 버전을 받아 'install' 명령으로 스스로 설치시킨다 (install_app()과 동일한, 이미 검증된 경로).
    지금 떠 있는 감시기 프로세스 '안'에서 직접 자기 파일을 덮어쓰면, 정리 과정에서 스스로를 닫아버려
    복사가 끝나기 전에 죽을 수 있다. 그래서 받은 exe를 별도 프로세스로 띄워 그쪽이 기존 걸 정리하고
    설치하게 한다 (완전히 다른 PID라 자기 자신을 건드릴 위험이 없다)."""
    try:
        req = urllib.request.Request(download_url, headers={"User-Agent": UPDATE_USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if len(data) < 1_000_000 or data[:2] != b"MZ":  # 너무 작거나 exe 형식이 아니면 받다 만 것
            return False
        tmp = _app_root("TEMP") / f"intri_update_{secrets.token_hex(4)}.exe"
        tmp.write_bytes(data)
        DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([str(tmp), "install", "/S"], creationflags=DETACHED, close_fds=True)
        return True
    except Exception:
        return False

def _maybe_auto_update():
    """하루 한 번만 확인한다. 실패해도(네트워크 없음 등) 조용히 넘어간다 - 출근 체크를 방해하면 안 된다."""
    if update_already_checked_today():
        return
    mark_update_checked()
    newer, url, _tag = check_for_update()
    if newer and url:
        apply_update(url)

def install_app(confirm=False, silent=False):
    """EXE 자신을 %LOCALAPPDATA%에 복사하고, 로그온 자동 실행/바로가기/설치된 앱 항목을 등록한다.
    다시 실행하면 업데이트가 된다 (설정·로그인 세션은 그대로 유지)."""
    import winreg
    if not _is_frozen():
        if not silent: _msgbox("EXE로 빌드된 파일에서만 설치할 수 있습니다.", 0x10)
        return False
    if confirm and not _ask_yesno(f"{APP_NAME}을(를) 이 PC에 설치할까요?\n\nWindows에 로그인하면 자동으로 실행되어\n출근 체크와 퇴근 확인을 도와줍니다."):
        return False
    try:
        was_setup = load_config().get("setup_complete", False)
        _clean_relay_copies()
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        src = Path(sys.executable).resolve()
        if src != INSTALLED_EXE.resolve():
            # 감시기만 닫아서는 부족하다 - 상태창/설정창 등 뭐가 열려 있든 exe 파일을 잠그고
            # 있으면 덮어쓰기가 안 된다. 실행 중인 모든 인스턴스를 정리한 뒤에 복사한다.
            if INSTALLED_EXE.exists():
                _stop_all_instances(INSTALLED_EXE)
            for _ in range(20):  # 그래도 파일 핸들이 늦게 풀릴 수 있어 잠깐 재시도
                try:
                    shutil.copy2(src, INSTALLED_EXE)
                    break
                except PermissionError:
                    time.sleep(0.5)
            else:
                raise PermissionError("설치 위치의 프로그램이 아직 실행 중입니다. 작업 관리자에서 종료한 뒤 다시 시도해 주세요.")

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{INSTALLED_EXE}" watch')
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            for name, value in (("DisplayName", APP_NAME), ("DisplayVersion", APP_VERSION),
                                ("Publisher", "인트리홀딩스"), ("InstallLocation", str(INSTALL_DIR)),
                                ("DisplayIcon", str(INSTALLED_EXE)),
                                ("UninstallString", f'"{INSTALLED_EXE}" uninstall')):
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        try:
            _create_shortcut(_desktop_dir() / f"{APP_NAME}.lnk", INSTALLED_EXE, workdir=INSTALL_DIR, description=APP_NAME)
        except Exception:
            pass  # 바로가기는 편의 기능: 실패해도 설치는 계속

        DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([str(INSTALLED_EXE), "watch"], creationflags=DETACHED, close_fds=True, cwd=str(INSTALL_DIR))
        if not was_setup and not silent:
            subprocess.Popen([str(INSTALLED_EXE), "setup"], creationflags=DETACHED, close_fds=True, cwd=str(INSTALL_DIR))
        if not silent:
            if was_setup:
                _msgbox("업데이트가 완료되었습니다.\n기존 로그인·회사망 설정은 그대로 유지됩니다.")
            else:
                _msgbox("설치가 완료되었습니다.\n\n이어서 열리는 창에서 하이웍스 아이디/비밀번호를 한 번만 입력해 주세요.\n(반드시 사무실 네트워크에서 진행해 주세요 — 이때 접속한 네트워크가 '회사망'으로 등록됩니다)")
        return True
    except Exception as e:
        if not silent: _msgbox(f"설치에 실패했습니다.\n\n{e}", 0x10)
        return False

def uninstall_app(silent=False, relay=False):
    """자동 실행 등록·바로가기·설치 파일·저장된 로그인 세션/설정을 모두 지운다."""
    import winreg
    if not silent and not relay and not _ask_yesno(f"{APP_NAME}을(를) 제거할까요?\n\n자동 실행 등록, 바탕화면 아이콘,\n저장된 로그인 세션과 설정이 모두 삭제됩니다."):
        return False
    if _running_from_install_dir() and not relay:
        # 설치된 exe는 자기 자신을 지울 수 없으니, 임시 폴더로 복사한 사본이 대신 지운다.
        tmp = _app_root("TEMP") / f"intri_uninstall_{os.getpid()}.exe"
        shutil.copy2(sys.executable, tmp)
        relay_args = [str(tmp), "uninstall", "/relay"] + (["/S"] if silent else [])
        subprocess.Popen(relay_args, creationflags=0x00000008 | 0x00000200, close_fds=True)
        return True
    if INSTALLED_EXE.exists():
        _stop_all_instances(INSTALLED_EXE)  # 감시기뿐 아니라 열려 있는 모든 창/프로세스 정리
    for parent, sub in ((RUN_KEY, RUN_VALUE),):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, parent, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, sub)
        except OSError:
            pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    try:
        (_desktop_dir() / f"{APP_NAME}.lnk").unlink(missing_ok=True)
    except Exception:
        pass
    # 브라우저 프로필이 잠깐 잠겨 있을 수 있고(방금 닫힌 Edge), 설치 폴더는 원래 exe가
    # 끝날 때까지 잠겨 있다. 둘 다 잠시 재시도한다.
    for _ in range(20):
        shutil.rmtree(APP_DIR, ignore_errors=True)
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
        if not APP_DIR.exists() and not INSTALL_DIR.exists():
            break
        time.sleep(0.5)
    _clean_relay_copies()
    leftovers = [str(p) for p in (INSTALL_DIR, APP_DIR) if p.exists()]
    if not silent:
        if leftovers:
            _msgbox("제거를 마쳤지만 아래 폴더가 남아 있습니다. 직접 삭제해 주세요.\n\n"
                    + "\n".join(leftovers), 0x30)
        else:
            _msgbox("제거가 완료되었습니다.")
    return not leftovers


# ---------- 진입점 ----------

def main():
    args = [a.lower() for a in sys.argv[1:]]
    cmd = args[0] if args else None
    silent = "/s" in args

    if cmd is None:
        if _is_frozen() and not _running_from_install_dir():
            # 배포 폴더에서 EXE를 그냥 더블클릭하면 설치 프로그램으로 동작한다.
            sys.exit(0 if install_app(confirm=True) else 1)
        # 로그인 정보가 저장돼 있으면 상태 화면(자동 최소화), 아니면 등록 화면.
        cmd = "status" if has_credentials() else "setup"

    if cmd == "install":
        sys.exit(0 if install_app(silent=silent) else 1)
    elif cmd == "uninstall":
        sys.exit(0 if uninstall_app(silent=silent, relay="/relay" in args) else 1)
    elif cmd == "setup":
        setup_gui()
    elif cmd == "status":
        status_window()
    elif cmd == "checkin":
        cfg = load_config()
        ok, msg = run_attend("checkin", headless=not cfg.get("debug_show_browser", False))
        print(msg); sys.exit(0 if ok else 1)
    elif cmd == "checkout":
        cfg = load_config()
        ok, msg = run_attend("checkout", headless=not cfg.get("debug_show_browser", False))
        print(msg); sys.exit(0 if ok else 1)
    elif cmd == "watch":
        watch_shutdown()

if __name__ == "__main__":
    main()
