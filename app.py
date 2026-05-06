import streamlit as st
import pandas as pd
from datetime import datetime, timedelta, time
from dateutil import tz


# --- Settings you can tweak ---
DATA_XLSX = "RouteSchedule.xlsx"
MIN_TRANSFER_SECONDS = 5 * 60      # minimum minutes to transfer between trips at same stop
HOURS_LOOKAHEAD = 10 * 24          # search window (hours) across multiple days
TZ = tz.gettz("America/Chicago")   # your timezone
LOGO_PATH = "AMS logo_NoTag.png"   # place this image file next to app.py

STORES_CSV = "stores.csv"       # mapping of branch codes to friendly names & search aliases

OPEN_TIME = time(7, 30)          # default 'ready for pickup' opening time (07:30 local)

# Night-order rule: orders placed at/after this time are considered "night"
NIGHT_ORDER_CUTOFF = time(18, 0)  # 6:00 PM local

# If an order is placed at/after NIGHT_ORDER_CUTOFF, the earliest NEXT-DAY departure
# allowed from the origin must be at/after the time specified here (per origin).
# This lets us model that the first morning truck is preloaded from previous-day orders.
ORIGIN_NEXTDAY_MIN_DEP = {
    "BR60": time(9, 0),   # BR60: skip 08:00 next-day departure for night orders; earliest is 09:00
    # add more origins as needed, e.g. "BR30": time(11, 15),
}

# Optional per-route override: (origin, dest) -> earliest NEXT-DAY departure time for night orders
# Use this to allow exceptions for specific lanes, e.g., BR60→BR64 can still catch 08:00.
ROUTE_NEXTDAY_MIN_DEP = {
    ("BR60", "BR64"): time(8, 0),
    # add more pair-specific overrides as needed
}

DC_ORIGINS = {"BR60", "BR30", "BR83", "BR51"}  # set of branch codes that are DC origins
DAY_METHODS = {"SM", "EM", "LM", "SHU"}        # methods treated as daytime runs that can hand off to NT after last departure

# BR30 special rule (gateway):
# In the real world, any BR30 freight that will ultimately be handled by BR60/BR83
# does NOT ride the BR30→BR51 night truck. It must leave BR30 on the LM shuttle to BR34,
# where it meets BR60.
BR30_BR60_GATEWAY_STOP = "BR34"
BR30_BR60_GATEWAY_METHOD = "LM"

# Branch equivalents: treat these codes as the same physical node for routing.
# IMPORTANT: values must be canonicalized (no leading zeros): BR01 -> BR1
# BR01 is an alias for BR30. canonical_br('BR01') => 'BR1', so map BR1 -> BR30.
BR_EQUIV = {
    "BR61": "BR60",
    "BR1": "BR30",
}

# ---------- Helpers ----------
def route_node(code: str) -> str:
    """Map a user-selected code to the canonical routing node (handles equivalents)."""
    code = canonical_br(code)
    return canonical_br(BR_EQUIV.get(code, code))
# --- Branch directory (code ↔ friendly name, plus search aliases) ---

def _norm(s: str) -> str:
    """Normalize free-form user input / aliases for matching."""
    if s is None:
        return ""
    s = str(s).strip().lower()
    # strip common filler words
    for prefix in ("the ",):
        if s.startswith(prefix):
            s = s[len(prefix):]
    # drop punctuation and extra spaces
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace()).strip()
    return s

# --- Canonicalize branch codes to "BR{int}" (e.g., BR03, 03, 3 -> BR3) ---
def canonical_br(code) -> str:
    """Convert codes like 'BR03', '03', 3 -> 'BR3' (no leading zeros)."""
    if code is None:
        return ""
    # Handle pandas NA
    try:
        if pd.isna(code):
            return ""
    except Exception:
        pass

    s = str(code).strip().upper()
    if not s:
        return ""

    # Extract digits from anything like BR03, BR 03, 03, etc.
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        # Fallback: return cleaned string
        return s

    return f"BR{int(digits)}"

# Display helper for branch codes

def display_br(code, width: int = 2) -> str:
    """Display branch codes as BR01/BR02/etc while keeping internal codes as BR1/BR2."""
    c = canonical_br(code)
    if c.startswith("BR") and c[2:].isdigit():
        return f"BR{int(c[2:]):0{width}d}"
    return c


# --- Store closing time parser ---
def parse_clock_time(val):
    """Parse a clock time from CSV cells. Accepts '17:30', '5:30 PM', datetime/time."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except Exception:
        pass

    # direct time-like types
    try:
        if isinstance(val, time):
            return time(val.hour, val.minute)
    except Exception:
        pass

    try:
        if isinstance(val, (pd.Timestamp, datetime)):
            t = val.time()
            return time(t.hour, t.minute)
    except Exception:
        pass

    s = str(val).strip()
    if not s:
        return None

    import re
    # 12-hour like 5:30 PM
    m = re.match(r'^(\d{1,2}):(\d{2})\s*([AaPp][Mm])$', s)
    if m:
        hh = int(m.group(1)); mm = int(m.group(2)); ap = m.group(3).upper()
        if not (1 <= hh <= 12 and 0 <= mm <= 59):
            return None
        if ap == "PM" and hh != 12:
            hh += 12
        if ap == "AM" and hh == 12:
            hh = 0
        return time(hh, mm)

    # 24-hour like 17:30
    m = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if m:
        hh = int(m.group(1)); mm = int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return time(hh, mm)

    return None

@st.cache_data(show_spinner=False)
def load_stores(csv_path: str):
    """
    Returns:
      code_to_name: dict like {"BR60": "Sioux City (DC)"}
      alias_index: dict normalized_alias -> "BR60"
      close_times: dict like {"BR60": {"mf": time(...), "sat": time(...)}, ...}
    The CSV can have flexible headers. Expected columns (any one from each group):
      - Code:  one of ["Code","Branch","Branch_ID","Stop_ID","Store","Store_ID","BR","br","StopId"]
      - Name:  one of ["Name","Store_Name","Branch_Name","Location","City","Display","Friendly","Store Name"]
      - Number (optional if Code already has BRxx): one of ["Number","No","Branch_Number"]
      - Close_MF, Close_Sat: optional closing times (see parse_clock_time)
    """
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        # If not found, fall back to empty directory
        return {}, {}, {}

    # Header normalization
    remap = {c.lower().strip(): c for c in df.columns}
    def _pick(cands):
        for c in cands:
            cc = remap.get(c.lower())
            if cc:
                return cc
        return None

    col_code = _pick(["Code","Branch","Branch_ID","Stop_ID","Store","Store_ID","BR","br","StopId"])
    col_name = _pick(["Name","Store_Name","Branch_Name","Location","City","Display","Friendly","Store Name"])
    col_num  = _pick(["Number","No","Branch_Number","Store_Number"])

    # Optional close-time columns
    col_close_mf  = _pick(["Close_MF", "Close", "Closing", "Closing_MF", "MF_Close", "Weekday_Close", "MonFri_Close"])
    col_close_sat = _pick(["Close_Sat", "Sat_Close", "Saturday_Close", "Closing_Sat"])

    code_to_name: dict[str, str] = {}
    alias_index: dict[str, str] = {}
    close_times = {}

    for _, row in df.iterrows():
        # Build canonical code like "BR60" (robust against spaces/hyphens like "BR 60" or numeric-only ids like "60")
        code_raw = (row.get(col_code, "") or "").strip() if col_code else ""
        num_raw = (row.get(col_num, "") or "").strip() if col_num else ""
        name_raw = str(row.get(col_name, "")).strip() if col_name else ""
        # Prefer any digits found in either field
        digits = "".join(ch for ch in (code_raw + " " + num_raw) if ch.isdigit())
        code = ""
        if digits:
            code = f"BR{int(digits)}"
        else:
            # Fallback: normalize alphanumerics only and try to parse BR + digits
            cr = "".join(ch for ch in code_raw.upper() if ch.isalnum())
            if cr.startswith("BR") and cr[2:].isdigit():
                code = f"BR{int(cr[2:])}"

        if not code:
            continue

        # Optional close times
        close_mf = parse_clock_time(row.get(col_close_mf)) if col_close_mf else None
        close_sat = parse_clock_time(row.get(col_close_sat)) if col_close_sat else None
        if close_mf or close_sat:
            close_times[code] = {"mf": close_mf, "sat": close_sat}

        # Friendly name (fallback to code)
        name = name_raw if name_raw else code
        code_to_name[code] = name

        # Build aliases:
        aliases = set()
        aliases.add(code)                              # "BR60"
        if code[2:].isdigit():
            aliases.add(code[2:])                      # "60"
        # variations like "br 60"
        aliases.add(code[:2].lower() + code[2:])       # "br60"
        if name_raw:
            nm = name_raw.strip()
            aliases.add(nm)                            # "Merrill Company"
            aliases.add(nm.lower())                    # case-insensitive
            # Strip "company" suffix and common fillers
            nm2 = nm.lower().replace(" company", "").strip()
            aliases.add(nm2)
            aliases.add(nm2.replace("the ", ""))

        # Index all normalized aliases
        for a in aliases:
            na = _norm(a)
            if not na:
                continue
            # Prefer the first-seen code for an alias; don't overwrite in case of duplicates
            alias_index.setdefault(na, code)

    return code_to_name, alias_index, close_times


# --- Google sign-in helpers (inserted after load_stores) ---
def email_alias_key(value: str) -> str:
    """Normalize a store email prefix or branch name for matching."""
    value = (value or "").strip().lower()
    if "@" in value:
        value = value.split("@", 1)[0]
    return "".join(ch for ch in value if ch.isalnum())

EMAIL_BRANCH_DEFAULTS = {
    "apw": "BR61",
    "rockisland": "BR80",
    "grandislanddc": "BR83",
    "grandisland": "BR77",
    "ips": "BR91",
    "merrill": "BR30",
}

def default_branch_from_email(email: str, stops: list[str], code_to_name: dict[str, str]) -> str | None:
    """Match company store emails like glenwood@domain.com to a branch code."""
    email_key = email_alias_key(email)
    if not email_key:
        return None

    # First, check explicit overrides for email prefixes that do not match the store name exactly.
    mapped_code = EMAIL_BRANCH_DEFAULTS.get(email_key)
    if mapped_code:
        mapped_code = canonical_br(mapped_code)
        if mapped_code in stops:
            return mapped_code

    # Then fall back to automatic matching against branch name/code.
    for code in stops:
        name = code_to_name.get(code, code)
        possible_keys = {
            email_alias_key(name),
            email_alias_key(display_br(code)),
            email_alias_key(code),
        }
        if email_key in possible_keys:
            return code

    return None


def auth_is_configured() -> bool:
    """Return True only when Streamlit Google auth secrets are present."""
    try:
        auth_config = st.secrets.get("auth")
        google_config = auth_config.get("google") if auth_config else None
        if not auth_config or not google_config:
            return False
        return bool(
            auth_config.get("redirect_uri")
            and auth_config.get("cookie_secret")
            and google_config.get("client_id")
            and google_config.get("client_secret")
            and google_config.get("server_metadata_url")
        )
    except Exception:
        return False


def current_google_email() -> str | None:
    """Return the signed-in Google email when Streamlit auth is configured and active."""
    try:
        if auth_is_configured() and getattr(st.user, "is_logged_in", False):
            return st.user.get("email")
    except Exception:
        pass
    return None


ALLOWED_GOOGLE_DOMAINS = {"arnoldgroupweb.com", "arnoldmotorsupply.com"}


def google_email_domain(email: str) -> str:
    """Return the lowercase domain from an email address."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[1]


def google_email_is_allowed(email: str) -> bool:
    """Allow only Arnold Motor Supply / Arnold Group Google accounts."""
    return google_email_domain(email) in ALLOWED_GOOGLE_DOMAINS


def render_account_footer():
    """Render Google sign-in controls below the branch dropdowns."""
    st.markdown("---")
    st.markdown("<div class='account-footer'>", unsafe_allow_html=True)

    if auth_is_configured():
        footer_email = current_google_email()
        if footer_email:
            if google_email_is_allowed(footer_email):
                st.caption(f"Signed in as {footer_email}")
            else:
                st.warning(
                    "You are signed in, but this app only uses Arnold Motor Supply / Arnold Group email accounts "
                    "to default the receiving branch. Please sign in with an @arnoldgroupweb.com or "
                    "@arnoldmotorsupply.com account."
                )
            if st.button("Sign out", key="google_logout_footer_btn"):
                st.logout()
        else:
            st.caption("Sign in with your Arnold Google account to default your receiving branch.")
            if st.button("Sign in with Google", key="google_login_footer_btn"):
                st.login("google")
    else:
        st.caption("Google sign-in is not configured yet. Receiving Branch can still be selected manually.")

    st.markdown("</div>", unsafe_allow_html=True)


def require_allowed_google_account():
    """Block the app unless the user is signed in with an allowed company Google account."""
    if not auth_is_configured():
        st.error("Google sign-in is not configured. Please configure app secrets before using this app.")
        st.stop()

    email = current_google_email()

    if not email:
        st.markdown(
            "<div class='arrival-card'>Sign in with your Arnold Google account to use this app.</div>",
            unsafe_allow_html=True,
        )
        if st.button("Sign in with Google", key="google_login_gate_btn"):
            st.login("google")
        st.stop()

    if not google_email_is_allowed(email):
        st.error(
            "Access denied. This app is only available to Arnold Motor Supply / Arnold Group Google accounts. "
            "Please sign out and sign in with an @arnoldgroupweb.com or @arnoldmotorsupply.com account."
        )
        st.caption(f"Currently signed in as {email}")
        if st.button("Sign out", key="google_logout_gate_btn"):
            st.logout()
        st.stop()

def autocomplete_options(query: str,
                         alias_index: dict[str, str],
                         code_to_name: dict[str, str],
                         stops: list[str],
                         exclude: str | None = None,
                         limit: int | None = None):
    """
    Returns a list of (display_label, value_code) pairs for st_searchbox.
    - When the search box is empty, show the full branch list in branch-number order.
    - When typing, matches aliases (e.g., 'merrill', '30', 'br60') via alias_index.
    - Prefers startswith → contains.
    - Adds smart guesses (digits → BRnn).
    - Excludes a specific code when needed.
    """
    def _branch_sort_key(code: str):
        c = canonical_br(code)
        if c.startswith("BR") and c[2:].isdigit():
            return (0, int(c[2:]))
        return (1, c)

    qn = _norm(query)

    # If the user has not typed anything yet, show all available branches in order.
    if not qn:
        ordered_stops = sorted(
            [s for s in stops if not exclude or s != exclude],
            key=_branch_sort_key,
        )
        return [
            (f"{display_br(code)} — {code_to_name.get(code, code)}", code)
            for code in ordered_stops
        ]

    # If input exactly names a known alias or exact code, return that single choice.
    raw = (query or "").strip()
    exact_code = alias_index.get(qn)

    # Direct BR/number patterns
    if not exact_code:
        if raw.isdigit():
            cand = f"BR{int(raw)}"
            if cand in stops and (not exclude or cand != exclude):
                exact_code = cand
        elif raw.upper().startswith("BR") and raw[2:].isdigit():
            cand = f"BR{int(raw[2:])}"
            if cand in stops and (not exclude or cand != exclude):
                exact_code = cand

    if exact_code and exact_code in stops and (not exclude or exact_code != exclude):
        label = f"{display_br(exact_code)} — {code_to_name.get(exact_code, exact_code)}"
        return [(label, exact_code)]

    # Smart guess when user types digits or BRnn
    guesses = []
    if query.strip().isdigit():
        guesses.append(f"BR{int(query.strip())}")
    elif query.strip().upper().startswith("BR") and query.strip()[2:].isdigit():
        guesses.append(f"BR{int(query.strip()[2:])}")

    seen = set()
    out  = []

    # 0) Put the smart guesses first if valid
    for g in guesses:
        if g in stops and (not exclude or g != exclude):
            label = f"{display_br(g)} — {code_to_name.get(g, g)}"
            out.append((label, g))
            seen.add(g)

    # 1) alias startswith
    starts, contains = [], []
    for alias, code in alias_index.items():
        if code in seen:
            continue
        if code not in stops:
            continue
        if exclude and code == exclude:
            continue
        if alias.startswith(qn):
            starts.append(code)
        elif qn in alias:
            contains.append(code)

    ordered = []
    for code in starts + contains:
        if code not in seen:
            ordered.append(code)
            seen.add(code)

    if limit is None:
        limit = 50

    # Build labels
    for code in ordered[: max(0, limit - len(out))]:
        label = f"{display_br(code)} — {code_to_name.get(code, code)}"
        out.append((label, code))

    return out

def display_name_for(code: str, code_to_name: dict[str, str]) -> str:
    """Get a nice display name for a branch code."""
    return code_to_name.get(code, code)
def parse_hhmm(s):
    """Parse a time cell into seconds since midnight.
    Accepts:
      - 'HH:MM' or 'HH:MM:SS' strings
      - strings that include dates like '2025-10-15 23:10:00'
      - datetime.time, pandas.Timestamp, numpy.datetime64
      - Excel-coerced numbers are ignored (return None) unless they format as time strings
    """
    if s is None or (isinstance(s, float) and pd.isna(s)) or (isinstance(s, str) and s.strip() == "") or pd.isna(s):
        return None

    # 1) Direct type handling first
    try:
        # datetime.time
        if isinstance(s, time):
            return s.hour * 3600 + s.minute * 60 + s.second
    except Exception:
        pass

    try:
        # pandas.Timestamp or datetime
        if isinstance(s, (pd.Timestamp, datetime)):
            t = s.time()
            return t.hour * 3600 + t.minute * 60 + t.second
    except Exception:
        pass

    try:
        # numpy.datetime64
        import numpy as np
        if isinstance(s, np.datetime64):
            # convert to pandas Timestamp then extract
            ts = pd.to_datetime(s)
            t = ts.time()
            return t.hour * 3600 + t.minute * 60 + t.second
    except Exception:
        pass

    # 2) String handling (robust)
    try:
        s_str = str(s).strip()
        # Look for a time-like pattern anywhere in the string
        import re
        m = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', s_str)
        if m:
            h = int(m.group(1))
            mnt = int(m.group(2))
            sec = int(m.group(3)) if m.group(3) else 0
            if 0 <= h <= 24 and 0 <= mnt <= 59 and 0 <= sec <= 59:
                return (h % 24) * 3600 + mnt * 60 + sec
    except Exception:
        pass

    # 3) Fallback: unsupported format
    return None

def normalize_columns(df):
    # Build a case/space-insensitive header lookup
    remap = {c.lower().replace(" ", "_"): c for c in df.columns}

    def _pick(cands):
        for c in cands:
            cc = remap.get(c.lower())
            if cc:
                return cc
        return None

    # Accept common variants for each expected field
    col_trip = _pick(["trip_id"]) or "Trip_ID"
    col_stop = _pick(["stop_id"]) or "Stop_ID"
    col_arr  = _pick(["arrival_time"]) or "Arrival_Time"
    col_dep  = _pick(["departure_time"]) or "Departure_Time"
    col_seq  = _pick(["sequence"]) or "Sequence"
    col_days = _pick(["days_active"]) or "Days_Active"
    col_method = _pick(["method", "delivery_method", "method_code"]) or "Method"  # not required

    # Create a new DataFrame with canonical columns, filling missing ones with NA
    cols_map = {
        "Trip_ID": col_trip,
        "Stop_ID": col_stop,
        "Arrival_Time": col_arr,
        "Departure_Time": col_dep,
        "Sequence": col_seq,
        "Days_Active": col_days,
        "Method": col_method,
    }

    new_df = pd.DataFrame()
    for canonical, src in cols_map.items():
        if src in df.columns:
            new_df[canonical] = df[src]
        else:
            # Column not present on this sheet; fill with NA so downstream code can drop/ignore
            new_df[canonical] = pd.NA

    return new_df

def days_active_to_set(s):
    # Expect like "1,2,3,4,5" (Mon..Fri). We’ll strip spaces & ignore junk gracefully.
    if pd.isna(s): 
        return set()
    parts = str(s).replace(" ", "").split(",")
    out = set()
    for p in parts:
        if p.isdigit():
            v = int(p)
            if 1 <= v <= 7:
                out.add(v)
    return out

@st.cache_data(show_spinner=False)
def read_all_connections(xlsx_path):
    """Read all sheets and build trip 'connections' (legs) with timing."""
    xls = pd.ExcelFile(xlsx_path)
    connections = []  # list of dicts: from_stop, to_stop, dep_s, arr_s, trip_id, days(set)
    all_stops = set()

    for sheet in xls.sheet_names:
        df = pd.read_excel(xls, sheet_name=sheet)
        if df.empty: 
            continue
        df = normalize_columns(df)
        # Fill Trip_ID with sheet name if missing/blank
        df["Trip_ID"] = df["Trip_ID"].fillna(sheet).replace("", sheet)

        # Parse times/sequences/days
        df["dep_s"]  = df["Departure_Time"].apply(parse_hhmm)
        df["arr_s"]  = df["Arrival_Time"].apply(parse_hhmm)
        df["Sequence"] = pd.to_numeric(df["Sequence"], errors="coerce")
        df["days_set"] = df["Days_Active"].apply(days_active_to_set)

        # Keep rows with Stop_ID and sequence
        df = df.dropna(subset=["Stop_ID","Sequence"]).sort_values(["Trip_ID","Sequence"])

        # Build edges for consecutive stops within same Trip_ID
        for trip_id, grp in df.groupby("Trip_ID"):
            g = grp.sort_values("Sequence")
            rows = g.to_dict("records")
            # Detect "night tour" pattern:
            # - first row has Departure_Time only (dep_s set, arr_s NaN)
            # - last row has Arrival_Time only (arr_s set, dep_s NaN)
            # - all middle rows have both times blank
            is_night_tour = False
            if len(rows) >= 2:
                first_has_dep_only = (pd.notna(rows[0]["dep_s"]) and pd.isna(rows[0]["arr_s"]))
                last_has_arr_only  = (pd.notna(rows[-1]["arr_s"]) and pd.isna(rows[-1]["dep_s"]))
                middles_blank = True
                for mid in rows[1:-1]:
                    if pd.notna(mid["dep_s"]) or pd.notna(mid["arr_s"]):
                        middles_blank = False
                        break
                is_night_tour = first_has_dep_only and last_has_arr_only and middles_blank

            if is_night_tour:
                # Evenly distribute time between first departure and final arrival across all legs.
                dep0 = int(rows[0]["dep_s"])
                arrN = int(rows[-1]["arr_s"])
                # Handle across-midnight: if numeric arrival < departure, add 24h
                if arrN < dep0:
                    arrN += 24 * 3600
                legs = len(rows) - 1  # number of segments
                if legs > 0 and arrN > dep0:
                    segment = (arrN - dep0) / legs
                    for i in range(legs):
                        a = rows[i]
                        b = rows[i+1]
                        dep_s_raw = int(round(dep0 + i * segment))
                        arr_s_raw = int(round(dep0 + (i + 1) * segment))

                        # Preserve whether this segment occurs after midnight by storing day offsets.
                        dep_day_offset = dep_s_raw // (24 * 3600)
                        arr_day_offset = arr_s_raw // (24 * 3600)

                        # Store seconds-of-day (0..86399) plus the day offsets.
                        dep_s_mod = dep_s_raw % (24 * 3600)
                        arr_s_mod = arr_s_raw % (24 * 3600)
                        if not a["days_set"]:
                            continue
                        connections.append({
                            "trip_id": trip_id,
                            "from": route_node(a["Stop_ID"]),
                            "to":   route_node(b["Stop_ID"]),
                            "dep_s": dep_s_mod,
                            "arr_s": arr_s_mod,
                            "dep_day_offset": int(dep_day_offset),
                            "arr_day_offset": int(arr_day_offset),
                            "days":  set(a["days_set"]),
                            "method": str(a.get("Method") or "").strip().upper(),
                        })
                        all_stops.add(route_node(a["Stop_ID"]))
                        all_stops.add(route_node(b["Stop_ID"]))
                continue  # done with this trip_id group

            # Default: regular trips where dep/arr provided for each leg
            for i in range(len(rows) - 1):
                a = rows[i]
                b = rows[i + 1]
                if pd.isna(a["dep_s"]) or pd.isna(b["arr_s"]):
                    continue
                if not a["days_set"]:
                    continue

                connections.append({
                    "trip_id": trip_id,
                    "from": route_node(a["Stop_ID"]),
                    "to":   route_node(b["Stop_ID"]),
                    "dep_s": int(a["dep_s"]),
                    "arr_s": int(b["arr_s"]),
                    "dep_day_offset": 0,
                    "arr_day_offset": 0,
                    "days":  set(a["days_set"]),
                    "method": str(a.get("Method") or "").strip().upper(),
                })
                all_stops.add(route_node(a["Stop_ID"]))
                all_stops.add(route_node(b["Stop_ID"]))
    return connections, sorted(all_stops)

def to_abs(dt_local, seconds_since_midnight):
    base = dt_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(seconds=seconds_since_midnight)

def weekday_num(dt_local):
    # Monday=1 ... Sunday=7
    return (dt_local.weekday() + 1)


# --- Store closing time selection and business open ---
def store_close_time(code: str, dt_local: datetime, close_map: dict):
    """Return the store closing time for the given local date (Mon–Fri or Sat)."""
    code = canonical_br(code)
    info = close_map.get(code) or {}
    wd = weekday_num(dt_local)
    if wd == 6:  # Saturday
        return info.get("sat")
    if 1 <= wd <= 5:  # Mon–Fri
        return info.get("mf")
    return None


def next_business_open(dt_local: datetime, open_t: time = OPEN_TIME) -> datetime:
    """Advance to next non-Sunday day at open_t."""
    d = (dt_local + timedelta(days=1)).replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
    # skip Sundays
    for _ in range(7):
        if weekday_num(d) != 7:
            return d
        d = (d + timedelta(days=1)).replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
    return d

def expand_connections(conns, start_dt_local):
    """
    Turn repeating daily legs into absolute-timed legs over a lookahead window.
    Each input connection repeats on specified weekdays (days set).
    """
    out = []
    horizon = start_dt_local + timedelta(hours=HOURS_LOOKAHEAD)
    d = start_dt_local.replace(hour=0, minute=0, second=0, microsecond=0)

    while d <= horizon:
        wd = weekday_num(d)
        for c in conns:
            if wd in c["days"]:
                dep_abs = d + timedelta(days=int(c.get("dep_day_offset", 0)), seconds=c["dep_s"])
                arr_abs = d + timedelta(days=int(c.get("arr_day_offset", 0)), seconds=c["arr_s"])

                # Safety for non-offset legs that cross midnight
                if arr_abs < dep_abs:
                    arr_abs += timedelta(days=1)
                out.append({
                    "trip_id": c["trip_id"],
                    "from": c["from"],
                    "to": c["to"],
                    "dep": dep_abs,
                    "arr": arr_abs,
                    "method": c.get("method", ""),
                })
        d += timedelta(days=1)
    # Sort by (departure, arrival) so simultaneous departures prefer the quicker arrival
    out.sort(key=lambda x: (x["dep"], x["arr"]))
    return out

def earliest_arrival(
    origin,
    dest,
    start_dt_local,
    abs_legs,
    transfer_sec=MIN_TRANSFER_SECONDS,
):
    """
    Connection Scan Algorithm (CSA) earliest-arrival with transfer time.
    """
    if origin == dest:
        return start_dt_local, []

    best = {}           # stop -> earliest known arrival time
    prev = {}           # stop -> (prev_stop, leg)
    best[origin] = start_dt_local

    for leg in abs_legs:
        u = leg["from"]
        v = leg["to"]
        dep = leg["dep"]
        arr = leg["arr"]

        if u in best:
            # If we are continuing on the SAME trip_id, allow zero transfer buffer (stay on vehicle).
            # Also allow zero-dwell when dep == best[u] (helps night-tour edge-to-edge segments).
            same_trip_ok = False
            try:
                last_leg_to_u = prev[u][1]
                same_trip_ok = (last_leg_to_u["trip_id"] == leg["trip_id"])
            except Exception:
                same_trip_ok = False
            required_buffer = 0 if same_trip_ok else transfer_sec
            earliest_board = best[u] + timedelta(seconds=required_buffer)
            can_board = (earliest_board <= dep)

            # Additional business rule: for the FIRST leg leaving the origin,
            # if the departure date is LATER than the order date (e.g., overnight or weekend),
            # enforce an earliest NEXT-DAY departure cutoff. Prefer per-route (origin→FINAL dest)
            # override; otherwise fall back to per-origin rule.
            if can_board and u == origin:
                try:
                    if dep.date() > start_dt_local.date():
                        # Prefer lane-specific override keyed by final dest, not the first hop.
                        min_dep_time = ROUTE_NEXTDAY_MIN_DEP.get((origin, dest)) or ORIGIN_NEXTDAY_MIN_DEP.get(origin)
                        if min_dep_time and dep.time() < min_dep_time:
                            can_board = False
                except Exception:
                    pass

            if can_board:
                if (v not in best) or (arr < best[v]):
                    best[v] = arr
                    prev[v] = (u, leg)

        # Early exit optimization
        if dest in best and leg["dep"] > best[dest]:
            break

    if dest not in best:
        return None, None

    # Reconstruct path
    steps = []
    cur = dest
    while cur != origin and cur in prev:
        pr, leg = prev[cur]
        steps.append(leg)
        cur = pr
    steps.reverse()
    return best[dest], steps

def format_step(leg):
    return f"{leg['trip_id']}: {leg['from']} —[{leg['dep'].strftime('%a %Y-%m-%d %H:%M')}]→ {leg['to']} —[{leg['arr'].strftime('%a %Y-%m-%d %H:%M')}]"

# Validate a user-entered stop code or alias; offer suggestions
def pick_stop(user_text: str, label: str, stops_list: list[str], alias_index: dict[str, str], code_to_name: dict[str, str]):
    raw = (user_text or "").strip()
    s_norm = _norm(raw)
    if not s_norm:
        st.error(f"Enter a {label}.")
        st.stop()

    # Resolve via alias map first (lets users type '30', 'merrill', etc.)
    resolved = alias_index.get(s_norm)
    if not resolved:
        # Try simple BR-prefix guess if they typed digits
        if raw.isdigit():
            resolved = f"BR{int(raw)}"
        elif raw.upper().startswith("BR") and raw[2:].isdigit():
            resolved = f"BR{int(raw[2:])}"

    if resolved and resolved in stops_list:
        return resolved

    # Not resolved; propose friendly suggestions using aliases + names
    suggestions = suggest_matches(raw, alias_index, code_to_name, stops_list, limit=8)
    if suggestions:
        st.warning(f"{label.title()} '{raw}' not found. Did you mean: {', '.join(suggestions)}?")
    else:
        st.warning(f"{label.title()} '{raw}' is not in RouteSchedule.xlsx.")
    st.stop()

# --- Autocomplete helper ---
def suggest_matches(query: str, alias_index: dict[str, str], code_to_name: dict[str, str], stops: list[str], limit: int = 10):
    qn = _norm(query)
    if len(qn) < 2:
        return []
    # Priority: aliases that START with the query, then those that merely CONTAIN it
    starts, contains, seen = [], [], set()
    for alias, code in alias_index.items():
        if code not in stops:
            continue
        if alias.startswith(qn) and code not in seen:
            starts.append(code); seen.add(code)
        elif qn in alias and code not in seen:
            contains.append(code); seen.add(code)
        if len(starts) + len(contains) >= limit:
            break
    ordered = starts + contains
    return [f"{display_br(c)} — {code_to_name.get(c, c)}" for c in ordered[:limit]]

# ---------- UI ----------

st.set_page_config(page_title="When Will it Arrive?", page_icon="🚚", layout="centered")
st.markdown(
    """
    <style>
    :root{
      --page-bg:#eef4f7;
      --card-bg:#ffffff;
      --card-soft:#f7fafc;
      --text:#10202b;
      --muted:#5b6b78;
      --accent:#007897;
      --accent-dark:#005f78;
      --accent-soft:#e4f4f7;
      --success-bg:#e8f6ef;
      --success-border:#2f8f5b;
      --success-text:#0f5b3a;
      --warn:#d92f48;
      --input-bg:#ffffff;
      --input-border:#c8d7df;
      --shadow:0 18px 45px rgba(16, 32, 43, 0.10);
      --radius:18px;
    }

    html, body, [data-testid="stAppViewContainer"]{
      color-scheme: light !important;
      background:
        radial-gradient(circle at top left, rgba(0,120,151,0.12), transparent 32%),
        linear-gradient(180deg, #f6fbfd 0%, var(--page-bg) 100%) !important;
      color:var(--text) !important;
    }

    *{
      color-scheme: light !important;
    }

    [data-testid="stHeader"]{
      background: transparent !important;
    }

    [data-testid="stAppViewContainer"] > .main{
      padding-top:24px !important;
    }

    .block-container{
      max-width:860px !important;
      padding-top:24px !important;
      padding-bottom:48px !important;
    }

    /* Main app shell */
    .block-container > div:first-child{
      background:rgba(255,255,255,0.86);
      backdrop-filter:blur(10px);
      -webkit-backdrop-filter:blur(10px);
      border:1px solid rgba(200,215,223,0.78);
      border-radius:28px;
      box-shadow:var(--shadow);
      padding:28px 30px 32px 30px;
    }

    /* Logo area */
    [data-testid="stImage"]{
      margin-bottom:4px;
    }

    h1{
      text-align:center;
      color:var(--text) !important;
      font-size:2.15rem !important;
      font-weight:850 !important;
      letter-spacing:-0.035em;
      margin-top:6px !important;
      margin-bottom:26px !important;
    }

    

    /* Field group labels created with st.markdown */
    .stMarkdown p{
      color:var(--text);
    }

    .stTextInput > label,
    .stSelectbox > label,
    .stDateInput > label,
    label{
      color:var(--text) !important;
      font-weight:750 !important;
      opacity:1 !important;
      font-size:0.95rem !important;
    }

    /* Inputs and search boxes */
    .stTextInput input,
    div[data-baseweb="input"] input{
      background-color:var(--input-bg) !important;
      color:var(--text) !important;
      border-radius:14px !important;
      min-height:46px !important;
    }

    .stTextInput input,
    div[data-baseweb="input"]{
      border:1px solid var(--input-border) !important;
      box-shadow:0 1px 2px rgba(16,32,43,0.04) !important;
      background:var(--input-bg) !important;
      border-radius:14px !important;
    }

    .stTextInput input::placeholder,
    div[data-baseweb="input"] input::placeholder{
      color:#7b8b98 !important;
      opacity:1 !important;
    }

    .stTextInput input:focus,
    div[data-baseweb="input"]:focus-within{
      border-color:var(--accent) !important;
      box-shadow:0 0 0 3px rgba(0,120,151,0.15) !important;
      outline:none !important;
    }

    /* Selectboxes: force one consistent light display at all times */
    div[data-baseweb="select"],
    div[data-baseweb="select"] > div,
    div[data-baseweb="select"] div{
      background:#ffffff !important;
      color:var(--text) !important;
    }

    div[data-baseweb="select"] span,
    div[data-baseweb="select"] svg{
      color:var(--text) !important;
      fill:var(--text) !important;
      opacity:1 !important;
    }

    div[data-baseweb="select"] input,
    div[data-baseweb="select"] textarea,
    div[data-baseweb="select"] [contenteditable="true"]{
      background:#ffffff !important;
      color:var(--text) !important;
      caret-color:var(--text) !important;
      -webkit-text-fill-color:var(--text) !important;
      opacity:1 !important;
    }

    div[data-baseweb="select"] input::placeholder,
    div[data-baseweb="select"] textarea::placeholder{
      color:#7b8b98 !important;
      -webkit-text-fill-color:#7b8b98 !important;
      opacity:1 !important;
    }

    div[data-baseweb="popover"]{
      border-radius:14px !important;
      overflow:hidden !important;
      box-shadow:0 14px 34px rgba(16,32,43,0.16) !important;
      background:#ffffff !important;
    }

    ul[role="listbox"]{
      border-radius:14px !important;
      border:1px solid var(--input-border) !important;
      background:#ffffff !important;
      color:var(--text) !important;
    }

    li[role="option"],
    div[role="option"]{
      background:#ffffff !important;
      color:var(--text) !important;
      font-weight:700 !important;
      padding-top:10px !important;
      padding-bottom:10px !important;
    }

    li[role="option"] *,
    div[role="option"] *{
      color:var(--text) !important;
      opacity:1 !important;
    }

    li[role="option"]:hover,
    div[role="option"]:hover,
    li[aria-selected="true"],
    div[aria-selected="true"]{
      background:var(--accent-soft) !important;
      color:var(--accent-dark) !important;
    }

    li[role="option"]:hover *,
    div[role="option"]:hover *,
    li[aria-selected="true"] *,
    div[aria-selected="true"] *{
      color:var(--accent-dark) !important;
    }

    /* Prominent arrival card */
    .arrival-card{
      border:1px solid rgba(47,143,91,0.35);
      background:
        linear-gradient(135deg, rgba(232,246,239,1) 0%, rgba(246,253,249,1) 100%);
      padding:22px 24px;
      border-radius:var(--radius);
      font-size:25px;
      font-weight:760;
      line-height:1.28;
      box-shadow:0 10px 26px rgba(47,143,91,0.12);
      margin-top:8px;
      margin-bottom:12px;
      color:#123026;
    }

    .arrival-card .eta{
      color:var(--success-text);
      font-weight:900;
    }

    .arrival-card .date{
      color:#41545f;
      font-weight:650;
    }

    /* Cutoff message just below ETA */
    .order-cutoff{
      color:var(--warn);
      background:#fff2f4;
      border:1px solid rgba(217,47,72,0.18);
      border-radius:14px;
      padding:12px 14px;
      font-size:17px;
      margin-top:10px;
      margin-bottom:10px;
      font-weight:650;
    }

    /* Delivery method note */
    .method-note{
      color:var(--text);
      background:var(--accent-soft);
      border:1px solid rgba(0,120,151,0.18);
      border-radius:14px;
      padding:12px 14px;
      font-size:17px;
      margin-top:8px;
      margin-bottom:10px;
      font-weight:600;
    }

    .method-note b{
      color:var(--accent-dark);
      font-size:22px;
      font-weight:900;
      letter-spacing:0.02em;
    }

    .stCaption,
    [data-testid="stCaptionContainer"]{
      color:var(--muted) !important;
      font-size:0.9rem !important;
    }

    hr{
      border:none !important;
      border-top:1px solid rgba(91,107,120,0.18) !important;
      margin:22px 0 !important;
    }

    /* Route timeline */
    .timeline{
      position:relative;
      margin:18px 0 8px 0;
      padding:8px 0 4px 24px;
      background:var(--card-soft);
      border:1px solid rgba(200,215,223,0.7);
      border-radius:16px;
    }

    .timeline::before{
      content:"";
      position:absolute;
      left:17px; top:18px; bottom:18px;
      width:2px; background:var(--accent);
      opacity:0.45;
    }

    .timeline-item{
      position:relative;
      margin:0 12px 15px 0;
      padding-left:18px;
    }

    .timeline-item::before{
      content:"";
      position:absolute;
      left:-11px; top:5px;
      width:12px; height:12px;
      border-radius:50%;
      background:var(--accent);
      box-shadow:0 0 0 4px rgba(0,120,151,0.13);
    }

    .timeline-title{
      font-weight:850;
      color:var(--accent-dark);
      margin-bottom:4px;
    }

    .timeline-meta{
      color:var(--muted);
      font-size:14px;
      font-weight:650;
      line-height:1.45;
    }

    /* High-contrast alerts */
    div[role="alert"]{
      background:#fff1f3 !important;
      border:1px solid rgba(217,47,72,0.32) !important;
      color:#2b0a0e !important;
      border-radius:14px !important;
      font-weight:650 !important;
      box-shadow:0 6px 16px rgba(217,47,72,0.08) !important;
    }

    div[role="alert"] p,
    div[role="alert"] span,
    div[role="alert"] li,
    div[role="alert"] *{
      color:#2b0a0e !important;
      opacity:1 !important;
    }

    div[role="alert"] [data-testid="stIconContainer"] svg{
      color:var(--warn) !important;
    }

    /* Primary buttons */
    .stButton button{
      background-color:var(--accent) !important;
      border:1px solid var(--accent) !important;
      color:white !important;
      border-radius:14px !important;
      min-height:42px !important;
      font-weight:800 !important;
      box-shadow:0 8px 18px rgba(0,120,151,0.16) !important;
      transition:all 0.15s ease-in-out !important;
    }

    .stButton button:hover{
      background-color:var(--accent-dark) !important;
      border-color:var(--accent-dark) !important;
      transform:translateY(-1px);
    }

    /* Link-like route/custom toggles */
    .linklike > button{
      width:100%;
      text-align:center !important;
      background:#f4f8fa !important;
      border:1px solid rgba(0,120,151,0.18) !important;
      color:var(--accent-dark) !important;
      text-decoration:none !important;
      padding:10px 12px !important;
      font-weight:800 !important;
      box-shadow:none !important;
      border-radius:14px !important;
    }

    .linklike > button:hover{
      background:var(--accent-soft) !important;
      opacity:1 !important;
      transform:translateY(-1px);
    }

    /* Date/time chooser visual spacing */
    [data-testid="stDateInput"],
    [data-testid="stTextInput"],
    [data-testid="stSelectbox"]{
      margin-bottom:4px;
    }

    /* Hide Streamlit top toolbar, header chrome, and footer */
    header[data-testid="stHeader"]{ display:none !important; }
    div[data-testid="stToolbar"]{ display:none !important; }
    div#MainMenu{ visibility:hidden !important; }
    div[data-testid="stStatusWidget"]{ display:none !important; }
    div[data-testid="stDecoration"]{ display:none !important; }
    footer{ visibility:hidden !important; }

    @media (max-width: 640px){
      .block-container{
        padding:14px 12px 36px 12px !important;
      }
      .block-container > div:first-child{
        padding:20px 16px 24px 16px;
        border-radius:22px;
      }
      h1{
        font-size:1.78rem !important;
      }
      .arrival-card{
        font-size:21px;
        padding:18px;
      }
      .order-cutoff,
      .method-note{
        font-size:15px;
      }
      .method-note b{
        font-size:19px;
      }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# Header with logo
try:
    c1, c2, c3 = st.columns([1,3,1])
    with c2:
        st.image(LOGO_PATH, width='content')
except Exception:
    pass  # if the logo file is missing, proceed without blocking the app

st.title("When Should it Arrive?")

# Require a company Google account before showing the route lookup.
require_allowed_google_account()


# Load network
try:
    conns, stops = read_all_connections(DATA_XLSX)

    # Load branch directory / aliases and per-store close times
    code_to_name, alias_index, close_times = load_stores(STORES_CSV)

    # Allow equivalent branches to be selectable even if they aren't in the schedule stops list
    stops_ui = sorted(set(stops) | set(BR_EQUIV.keys()))

    if not conns:
        st.error("No connections found. Check column names and that sheets contain Trip_ID, Stop_ID, Arrival_Time, Departure_Time, Sequence, Days_Active.")
        st.stop()
except Exception as e:
    st.exception(e)
    st.stop()


def branch_dropdown_options(stops: list[str], code_to_name: dict[str, str], exclude: str | None = None):
    """Return branch dropdown labels and a label-to-code lookup, sorted by branch number."""
    def _branch_sort_key(code: str):
        c = canonical_br(code)
        if c.startswith("BR") and c[2:].isdigit():
            return (0, int(c[2:]))
        return (1, c)

    ordered_stops = sorted(
        [s for s in stops if not exclude or s != exclude],
        key=_branch_sort_key,
    )

    labels = [
        f"{display_br(code)} — {code_to_name.get(code, code)}"
        for code in ordered_stops
    ]

    lookup = dict(zip(labels, ordered_stops))
    return labels, lookup

def branch_label_for(code: str, code_to_name: dict[str, str]) -> str | None:
    """Return the dropdown label for a branch code."""
    if not code:
        return None
    code = canonical_br(code)
    return f"{display_br(code)} — {code_to_name.get(code, code)}"


def remember_branch_selection(widget_key: str, state_key: str, lookup: dict[str, str]):
    """Store the selected branch code separately so reruns do not wipe the choice."""
    selected_label = st.session_state.get(widget_key)
    if selected_label:
        selected_code = lookup.get(selected_label)
        if selected_code:
            st.session_state[state_key] = selected_code


# Use the signed-in company email to default the receiving branch.
google_email = current_google_email()

if google_email and not st.session_state.get("selected_dest_code"):
    default_dest = default_branch_from_email(google_email, stops_ui, code_to_name)
    if default_dest:
        st.session_state["selected_dest_code"] = default_dest

origin_labels, origin_lookup = branch_dropdown_options(stops_ui, code_to_name)

saved_origin = st.session_state.get("selected_origin_code")
saved_origin_label = branch_label_for(saved_origin, code_to_name)
origin_index = origin_labels.index(saved_origin_label) if saved_origin_label in origin_labels else None

col1, col2 = st.columns(2)

with col1:
    origin_label = st.selectbox(
        "Supplier Branch",
        origin_labels,
        index=origin_index,
        placeholder="Choose or search supplier branch…",
        key="origin_box",
        on_change=remember_branch_selection,
        args=("origin_box", "selected_origin_code", origin_lookup),
    )
    origin = origin_lookup.get(origin_label) if origin_label else st.session_state.get("selected_origin_code")

# Build the destination list after origin is selected so the same branch can be excluded.
dest_labels, dest_lookup = branch_dropdown_options(stops_ui, code_to_name, exclude=origin)

saved_dest = st.session_state.get("selected_dest_code")
saved_dest_label = branch_label_for(saved_dest, code_to_name)
dest_index = dest_labels.index(saved_dest_label) if saved_dest_label in dest_labels else None

with col2:
    dest_label = st.selectbox(
        "Receiving Branch",
        dest_labels,
        index=dest_index,
        placeholder="Choose or search receiving branch…",
        key="dest_box",
        on_change=remember_branch_selection,
        args=("dest_box", "selected_dest_code", dest_lookup),
    )
    dest = dest_lookup.get(dest_label) if dest_label else st.session_state.get("selected_dest_code")

# If the saved destination is no longer valid because it matches the selected supplier, clear it.
if dest and origin and dest == origin:
    st.session_state["selected_dest_code"] = None
    dest = None

# Validate selections
if not origin:
    st.warning("Pick an origin branch to see the ETA.")
    render_account_footer()
    st.stop()
if not dest:
    st.warning("Pick a destination branch to see the ETA.")
    render_account_footer()
    st.stop()

# Map equivalents (e.g., BR61 -> BR60, BR30 -> BR1) for routing
origin_node = route_node(origin)
dest_node = route_node(dest)

if origin == dest:
    st.error("Origin and destination cannot be the same.")
    st.stop()

# Also block equivalents (BR61 and BR60 are the same routing node, etc.)
if origin_node == dest_node:
    st.error("Origin and destination cannot be the same (some branches are routed as equivalents).")
    st.stop()

# Optional: show a note if we remapped either selection
#if origin != origin_node or dest != dest_node:
#    st.info(
#        f"Note: routing uses {display_br(origin_node)} for {display_br(origin)} and {display_br(dest_node)} for {display_br(dest)}."
#    )


# Determine start datetime: use custom selection from session state if set; otherwise "now"
if st.session_state.get("custom_dt_active") and st.session_state.get("custom_dt_value"):
    start_dt = st.session_state["custom_dt_value"]
else:
    start_dt = datetime.now(TZ)

# If the supplier (origin) is closed at the selected order time, treat the order as placed
# at the next business-day opening time for routing purposes.
routing_start_dt = start_dt
origin_close_t = store_close_time(origin_node, routing_start_dt, close_times)
if origin_close_t:
    close_dt_today = routing_start_dt.replace(hour=origin_close_t.hour, minute=origin_close_t.minute, second=0, microsecond=0)
    if routing_start_dt > close_dt_today:
        routing_start_dt = next_business_open(routing_start_dt, open_t=OPEN_TIME)
        # Origin is closed; we shift routing_start_dt to next business open (no UI warning shown).

# --- Auto-calculate ETA on load (no buttons) ---
# Defensive: disallow same origin/destination (including equivalents)
if origin_node == dest_node:
    st.error("Origin and destination cannot be the same. Please choose a different destination branch.")
    st.stop()


abs_legs = expand_connections(conns, routing_start_dt)


# --- BR30 gateway rule (ENFORCED): if the best path is entering the BR60/BR83 network,
# BR30 freight MUST first leave BR30 on the LM shuttle to BR34 (no BR30→BR51 night truck).
# If we can't find a feasible path under this constraint, we stop and tell you to fix the schedule.

def _m(x):
    return (x.get("method") or "").strip().upper()

def _is_blank_method(x) -> bool:
    return _m(x) == ""

def _passes_through(nodes_steps, node_code: str) -> bool:
    return any((l.get("from") == node_code or l.get("to") == node_code) for l in (nodes_steps or []))

def _first_touch_index(nodes_steps, node_code: str):
    """Return the first leg index where node_code is touched as from/to; None if absent."""
    for i, l in enumerate(nodes_steps or []):
        if l.get("from") == node_code or l.get("to") == node_code:
            return i
    return None

def _touches_before(nodes_steps, first: str, later_nodes: tuple[str, ...]) -> bool:
    """True if `first` appears in the path before any of `later_nodes`."""
    i_first = _first_touch_index(nodes_steps, first)
    if i_first is None:
        return False
    idxs = [_first_touch_index(nodes_steps, n) for n in later_nodes]
    idxs = [i for i in idxs if i is not None]
    return bool(idxs) and i_first < min(idxs)

# First pass: normal routing
eta, steps = earliest_arrival(
    origin_node,
    dest_node,
    routing_start_dt,
    abs_legs,
    transfer_sec=MIN_TRANSFER_SECONDS,
)

# If the normal route routes freight THROUGH BR30 and then into the BR60/BR83 network,
# enforce the BR30→BR34 (LM) gateway (blocks BR30→BR51 night truck for those paths).
if steps and any(l.get("from") == "BR30" for l in steps) and _touches_before(steps, "BR30", ("BR60", "BR83")):
    gateway_stop = BR30_BR60_GATEWAY_STOP        # BR34
    gateway_method = BR30_BR60_GATEWAY_METHOD    # LM

    # The LM shuttle is usually a multi-stop trip that LEAVES BR30 and eventually TOUCHES BR34.
    # Our schedule therefore may not have a direct BR30→BR34 leg; instead it can be BR30→...→BR34.
    # We enforce the rule by only allowing legs that depart BR30 on an LM (or blank-method) trip
    # that reaches BR34 somewhere in that same trip.

    # 1) Candidate trip_ids: anything that departs BR30 with the expected gateway method (or blank)
    candidate_trip_ids = set()
    for leg in abs_legs:
        if leg.get("from") == "BR30":
            if _m(leg) == gateway_method or _is_blank_method(leg):
                candidate_trip_ids.add(leg.get("trip_id"))

    # 2) Allowed trip_ids: candidate trips that touch the gateway stop BR34 anywhere
    allowed_trip_ids = set()
    for leg in abs_legs:
        tid = leg.get("trip_id")
        if tid in candidate_trip_ids and (leg.get("from") == gateway_stop or leg.get("to") == gateway_stop):
            allowed_trip_ids.add(tid)

    # If we can't find any such shuttle trip in the lookahead window, fail clearly.
    if not allowed_trip_ids:
        st.error(
            "BR30→BR60/BR83 freight must leave BR30 on the LM shuttle that meets BR60 at BR34, "
            "but no LM shuttle trip departing BR30 that reaches BR34 was found in the current schedule window. "
            "Check RouteSchedule.xlsx for a BR30 LM trip that runs BR30→…→BR34 on the appropriate day(s)/time(s)."
        )
        st.stop()

    # 3) Enforce: any leg that departs BR30 must be on one of the allowed shuttle trips.
    # This blocks the BR30→BR51 night truck while still allowing BR30→BR03→…→BR34 type routes.
    abs_legs_gateway = [
        leg for leg in abs_legs
        if not (leg.get("from") == "BR30" and leg.get("trip_id") not in allowed_trip_ids)
    ]

    eta2, steps2 = earliest_arrival(
        origin_node,
        dest_node,
        routing_start_dt,
        abs_legs_gateway,
        transfer_sec=MIN_TRANSFER_SECONDS,
    )

    if not eta2 or not steps2:
        st.error(
            "BR30→BR60/BR83 freight must leave BR30 on the LM shuttle that meets BR60 at BR34, "
            "but no feasible route was found under that rule within the lookahead window. "
            "Double-check that the BR30 LM shuttle connects onward to BR60 (and then to your destination) on the correct days."
        )
        st.stop()

    # Use the constrained path (this blocks the BR30→BR51 night truck in these cases)
    eta, steps = eta2, steps2


# Prefer a "ready" time without delivery-method special cases:
# - If arrival is before opening on that day → show OPEN_TIME that day
# - Else if same-trip departure from dest within 2h → use that departure
# - Else → raw arrival
eta_display = eta
if eta and steps:
    last_leg = steps[-1]
    dest_arr = last_leg["arr"]

    open_dt = dest_arr.replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute, second=0, microsecond=0)
    if dest_arr.time() < OPEN_TIME:
        eta_display = open_dt
    else:
        trip_id = last_leg["trip_id"]
        ready_window = timedelta(hours=2)
        candidates = [
            leg for leg in abs_legs
            if leg["trip_id"] == trip_id
            and leg["from"] == dest
            and leg["dep"] >= dest_arr
            and leg["dep"] <= dest_arr + ready_window
        ]
        if candidates:
            eta_display = min(candidates, key=lambda x: x["dep"])["dep"]
        else:
            eta_display = dest_arr

if not eta:
    st.error("No feasible path found within the lookahead window. Check schedules and Days_Active.")
else:
    origin_name = display_name_for(origin, code_to_name)
    dest_name = display_name_for(dest, code_to_name)

    # Suggest delivery method for DCs (BR60, BR30, BR83, BR51)
    # Base it on the first leg in the chosen earliest-arrival path that actually
    # departs from the DC origin, but if the overall route arrives in the
    # overnight window and contains an NT leg, prefer NT as the recommendation.
    delivery_hint = None
    if steps and origin_node in DC_ORIGINS:
        # Prefer the first leg in the chosen path whose 'from' is exactly the origin DC.
        origin_legs = [leg for leg in steps if leg.get("from") == origin_node]
        if origin_legs:
            first_leg = origin_legs[0]
        else:
            # Fallback: use the very first leg in the path.
            first_leg = steps[0]

        method_code = (first_leg.get("method") or "").strip().upper()

        if method_code:
            # If the route is effectively an overnight delivery (arrives between
            # 18:01 and 06:59 and we show a "by the time your store opens" message),
            # and any leg in the chosen path uses NT, then recommend NT regardless
            # of what the first hop's method is.
            step_methods = [(l.get("method") or "").strip().upper() for l in steps]
            if any(m == "NT" for m in step_methods):
                # We'll refine this after we compute overnight_msg/opening_dt_for_msg below.
                preferred_overnight_method = "NT"
            else:
                preferred_overnight_method = None

            # Business rule overrides:
            passes_through_br60 = any((l.get("from") == "BR60" or l.get("to") == "BR60") for l in steps)

            # 1) BR51 routes that hand off at the BR60 meetup (Atlantic) must be ordered as SHU,
            # even if the chosen path later contains an NT leg.
            if origin_node == "BR51" and passes_through_br60:
                method_code = "SHU"
                preferred_overnight_method = None

            # 2) If the route starts at BR30 and the chosen path passes through BR60,
            # the ordering method must be LM (even if there is an NT leg later).
            if origin_node == "BR30" and passes_through_br60:
                method_code = "LM"
                preferred_overnight_method = None

            # We'll finalize which code to display later once we know whether
            # overnight_msg is True. For now, stash the base method_code and any
            # preferred overnight method in session-local variables via closure.
            delivery_hint = {
                "base_method": method_code,
                "preferred_overnight": preferred_overnight_method,
            }
    # Determine if the raw destination arrival lands in the "overnight" window (18:01–06:59)
    # If so, we will phrase the message as "by the time your store opens on Day, Date".
    overnight_msg = False
    opening_dt_for_msg = None
    if steps:
        last_leg = steps[-1]
        dest_arr = last_leg["arr"]
        t = dest_arr.time()
        # Overnight window: 18:01–23:59 or 00:00–06:59
        arrives_evening = (t.hour > 18) or (t.hour == 18 and t.minute >= 1)
        arrives_early   = (t.hour < 7)    # 00:00–06:59
        if arrives_evening or arrives_early:
            overnight_msg = True
            if arrives_evening:
                # Opening is next calendar day at OPEN_TIME
                opening_dt_for_msg = (dest_arr + timedelta(days=1)).replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute, second=0, microsecond=0)
            else:
                # Arrived before opening; opening is same day at OPEN_TIME
                opening_dt_for_msg = dest_arr.replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute, second=0, microsecond=0)

    now_local = datetime.now(TZ)
    is_past_eta = (eta_display is not None) and (eta_display < now_local)
    arrival_day = eta_display.strftime('%A')
    arrival_time = eta_display.strftime('%I:%M %p').lstrip('0')

    if overnight_msg and opening_dt_for_msg:
        opening_day_text = opening_dt_for_msg.strftime('%A')
        opening_date_text = opening_dt_for_msg.strftime('%B %d, %Y')
        st.markdown(
            f"<div class='arrival-card'>"
            f"Your order will arrive by the time your store opens on "
            f"<span class='eta'>{opening_day_text}</span>"
            f" (<span class='date'>{opening_date_text}</span>)."
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        past_prefix = "should have arrived on" if is_past_eta else "should arrive on"
        st.markdown(
            f"<div class='arrival-card'>"
            f"Your order from {origin_name} {past_prefix} "
            f"<span class='eta'>{arrival_day} at {arrival_time}</span> "
            f"(<span class='date'>{eta_display.strftime('%B %d, %Y')}</span>)."
            f"</div>",
            unsafe_allow_html=True,
        )

    # Finalize delivery method hint based on whether this is an overnight-style delivery.
    if isinstance(delivery_hint, dict):
        base_method = delivery_hint.get("base_method")
        preferred_overnight_method = delivery_hint.get("preferred_overnight")
        method_to_show = base_method

        # If we are in the overnight window and have an NT leg in the path,
        # prefer NT as the recommended method.
        if overnight_msg and preferred_overnight_method:
            method_to_show = preferred_overnight_method

        if method_to_show:
            delivery_hint = (
                f"To get this ETA, submit your order from {origin_name} using "
                f"delivery method <b>{method_to_show}</b>."
            )
        else:
            delivery_hint = None

    # Show cutoff message: last time you can place the order and still catch the first leg
    if steps:
        first_leg = steps[0]
        cutoff_dt = first_leg["dep"] - timedelta(seconds=MIN_TRANSFER_SECONDS)
        # If the supplier closes earlier than the computed cutoff, clamp to closing time.
        close_t = store_close_time(origin_node, first_leg["dep"], close_times)
        if close_t:
            close_dt = first_leg["dep"].replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)
            if close_dt < cutoff_dt:
                cutoff_dt = close_dt
        # clamp to start time if transfer window would push cutoff before now
        cutoff_display = cutoff_dt.strftime('%a %B %d, %Y %I:%M %p')
        st.markdown(
            f"<div class='order-cutoff'>Order by <b>{cutoff_display}</b> to receive by this ETA.</div>",
            unsafe_allow_html=True,
        )

    if delivery_hint:
        st.markdown(f"<div class='method-note'>{delivery_hint}</div>", unsafe_allow_html=True)

    st.caption(f"(Note: This order ETA is an ESTIMATE only. Actual arrival could change depending on unforeseen circumstances.)")
    st.markdown("---")


    # --- Inline toggles row (link-like buttons; equal width) ---
    # Ensure consistent full-width styling for link-like buttons in columns
    st.markdown("<style>.linklike > button{width:100%;}</style>", unsafe_allow_html=True)

    # Read current states
    show_route = st.session_state.get("show_route_open", False)
    show_custom = st.session_state.get("show_custom_dt_open", False)

    # Render toggles side-by-side like the branch selectors
    c_left, c_right = st.columns(2)
    with c_left:
        st.markdown("<div class='linklike'>", unsafe_allow_html=True)
        if not show_route:
            if st.button("Wanna see the route your order takes?", key="show_route_open_btn"):
                st.session_state["show_route_open"] = True
                st.rerun()
        else:
            if st.button("Hide route", key="show_route_close_btn"):
                st.session_state["show_route_open"] = False
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    with c_right:
        st.markdown("<div class='linklike'>", unsafe_allow_html=True)
        if not show_custom:
            if st.button("Need to check a different date and time?", key="open_custom_dt"):
                st.session_state["show_custom_dt_open"] = True
                st.rerun()
        else:
            if st.button("Hide date & time chooser", key="close_custom_dt"):
                st.session_state["show_custom_dt_open"] = False
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    # --- Route timeline content (below the toggles row) ---
    if steps and st.session_state.get("show_route_open", False):
        parts = ["<div class='timeline'>"]
        for i, leg in enumerate(steps, start=1):
            from_name = display_name_for(leg['from'], code_to_name)
            to_name   = display_name_for(leg['to'], code_to_name)
            dep_txt   = leg["dep"].strftime("%a %b %d, %Y %I:%M %p")
            arr_txt   = leg["arr"].strftime("%a %b %d, %Y %I:%M %p")
            parts.append(
                f"<div class='timeline-item'>"
                f"<div class='timeline-meta'>Depart {display_br(leg['from'])} — {from_name} at {dep_txt}</div>"
                f"<div class='timeline-meta'>Arrive {display_br(leg['to'])} — {to_name} at {arr_txt}</div>"
                f"</div>"
            )
        try:
            last_leg = steps[-1]
            if eta_display and eta_display > last_leg["arr"]:
                parts.append(
                    f"<div class='timeline-item'>"
                    f"<div class='timeline-title'>Ready for pickup</div>"
                    f"<div class='timeline-meta'>{display_br(dest)} — {display_name_for(dest, code_to_name)} at {eta_display.strftime('%a %b %d, %Y %I:%M %p')}</div>"
                    f"</div>"
                )
        except Exception:
            pass
        parts.append("</div>")
        st.markdown("".join(parts), unsafe_allow_html=True)

    # --- Date/time chooser content (below the toggles row) ---
    if st.session_state.get("show_custom_dt_open", False):
        _now_local = datetime.now(TZ)
        # If the key exists but is None, fall back to now.
        active_dt = st.session_state.get("custom_dt_value") or _now_local
        default_date = active_dt.date()
        default_time = active_dt.time().replace(second=0, microsecond=0)

        test_date = st.date_input("Order date", value=default_date, key="order_date")
        # 12-hour time input (text) + AM/PM selector
        default_time12 = default_time.strftime("%I:%M")
        default_ampm = "PM" if default_time.hour >= 12 else "AM"
        time_str = st.text_input("Order time (hh:mm)", value=default_time12, key="order_time_text", placeholder="hh:mm")
        ampm = st.selectbox("AM/PM", ["AM", "PM"], index=(0 if default_ampm == "AM" else 1), key="order_ampm")

        def _parse_12h_time(hhmm: str, ampm_val: str):
            try:
                hhmm = (hhmm or "").strip()
                parts = hhmm.split(":")
                if len(parts) != 2:
                    return None
                h = int(parts[0])
                m = int(parts[1])
                if not (1 <= h <= 12 and 0 <= m <= 59):
                    return None
                if ampm_val.upper() == "PM" and h != 12:
                    h += 12
                if ampm_val.upper() == "AM" and h == 12:
                    h = 0
                return time(h, m)
            except Exception:
                return None

        parsed_time = _parse_12h_time(time_str, ampm)
        if parsed_time is None:
            st.warning("Please enter time as hh:mm (e.g., 09:05) and select AM/PM.")
            chosen_time = default_time
        else:
            chosen_time = parsed_time

        c1, c2 = st.columns([1,1])
        with c1:
            if st.button("Use this date & time", key="use_custom_dt_submit"):
                st.session_state["custom_dt_active"] = True
                st.session_state["custom_dt_value"] = datetime.combine(test_date, chosen_time).replace(tzinfo=TZ)
                st.rerun()
        with c2:
            if st.button("Use current time", key="reset_custom_dt"):
                st.session_state["custom_dt_active"] = False
                # Clear the saved custom dt; UI will safely fall back to now.
                st.session_state["custom_dt_value"] = None
                st.rerun()

    # Account controls live at the bottom so they do not interrupt the route lookup flow.
    render_account_footer()

    # In the unlikely case of zero steps (should only happen if origin == dest, which we block)
    pass

