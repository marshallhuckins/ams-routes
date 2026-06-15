import os
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta, time
from dateutil import tz


# --- Basic app settings ---
DATA_XLSX = "RouteSchedule.xlsx"
MIN_TRANSFER_SECONDS = 5 * 60      # minimum transfer buffer between trips at the same stop
HOURS_LOOKAHEAD = 10 * 24          # how far ahead the app searches for a route
TZ = tz.gettz("America/Chicago")   # local timezone for route calculations
LOGO_PATH = "AMS logo_NoTag.png"   # logo file used at the top of the app

STORES_CSV = "stores.csv"       # branch names, aliases, and closing times

OPEN_TIME = time(7, 30)          # default time we treat stores as open

# Orders placed at or after this time are treated as night orders.
NIGHT_ORDER_CUTOFF = time(18, 0)  # 6:00 PM local

# For night orders, some origins should skip their first next-day departure.
# Example: BR60's 8:00 AM truck may already be loaded, so the first usable truck is 9:00 AM.
ORIGIN_NEXTDAY_MIN_DEP = {
    "BR60": time(9, 0),   # night orders from BR60 start with the 9:00 AM truck
    # add more origins as needed, e.g. "BR30": time(11, 15),
}

# Route-specific night-order exceptions.
# Example: BR60→BR64 can still use the 8:00 AM truck.
ROUTE_NEXTDAY_MIN_DEP = {
    ("BR60", "BR64"): time(8, 0),
    # add more pair-specific overrides as needed
}

DC_ORIGINS = {"BR60", "BR30", "BR83", "BR51"}  # origins where we show a delivery-method hint
DAY_METHODS = {"SM", "EM", "LM", "SHU"}        # daytime route methods
INTERNAL_STOP_PREFIXES = ("BRM_", "MEET_")      # transfer-only stops hidden from dropdowns

# BR30/BR60 gateway rules:
# Freight from BR30 going into the BR60/BR83 network can use either:
# - the LM shuttle through BR34
# - the BR30 night truck that meets BR60 at BR81
# Freight from BR60 going into the BR30 network should use the new NT meetup through BR81.
# This keeps the app from choosing unrelated/older meetup routes while allowing the approved BR81 meetup.
BR30_BR60_LM_GATEWAY_STOP = "BR34"
BR30_BR60_NT_GATEWAY_STOP = "BR81"
BR30_BR60_LM_GATEWAY_METHOD = "LM"
BR30_BR60_NT_GATEWAY_METHOD = "NT"
BR60_BR30_NT_GATEWAY_STOP = "BR81"
BR60_BR30_NT_GATEWAY_METHOD = "NT"
BR60_BR30_NT_GATEWAY_TRIP_ID = "60_30_NT_MEETUP"

# Branch equivalents: these are separate branch codes, but they route like the same physical location.
# Keep the keys/values in canonical format with no leading zero, like BR1 instead of BR01.
BR_EQUIV = {
    "BR61": "BR60",
    "BR1": "BR30",
    "BR92": "BR60",
    "BR48": "BR34",
    "BR44": "BR43",
    "BR91": "BR43",
    "BR93": "BR80",
}

# ---------- Helper functions ----------
def route_node(code: str) -> str:
    """Return the routing node for a branch, including any equivalent-branch mapping."""
    code = canonical_br(code)
    return canonical_br(BR_EQUIV.get(code, code))
# --- Branch names and search aliases ---

def _norm(s: str) -> str:
    """Normalize user-entered search text so aliases match more reliably."""
    if s is None:
        return ""
    s = str(s).strip().lower()
    # Remove common filler words.
    for prefix in ("the ",):
        if s.startswith(prefix):
            s = s[len(prefix):]
    # Remove punctuation and clean up spacing.
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace()).strip()
    return s

# --- Branch code formatting ---
def canonical_br(code) -> str:
    """Convert branch codes like BR03, 03, or 3 into BR3 for internal use."""
    if code is None:
        return ""
    # Handle blank pandas values safely.
    try:
        if pd.isna(code):
            return ""
    except Exception:
        pass

    s = str(code).strip().upper()
    if not s:
        return ""

    # Pull the branch number out of values like BR03, BR 03, or 03.
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        # If there are no digits, return the cleaned value.
        return s

    return f"BR{int(digits)}"

# Display branch codes with leading zeroes when needed.

def display_br(code, width: int = 2) -> str:
    """Display internal branch codes as BR01, BR02, etc."""
    c = canonical_br(code)
    if c.startswith("BR") and c[2:].isdigit():
        return f"BR{int(c[2:]):0{width}d}"
    return c

def is_internal_route_stop(code: str) -> bool:
    """Return True for route-only transfer stops that should stay out of the dropdowns."""
    raw = "" if code is None else str(code).strip().upper()
    return raw.startswith(INTERNAL_STOP_PREFIXES)

# --- Store closing time helpers ---
def parse_clock_time(val):
    """Parse store closing times from the CSV."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except Exception:
        pass

    # Already a time-like value.
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
    # 12-hour format, like 5:30 PM.
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

    # 24-hour format, like 17:30.
    m = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if m:
        hh = int(m.group(1)); mm = int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return time(hh, mm)

    return None

@st.cache_data(show_spinner=False)
def load_stores(csv_path: str):
    """
    Read stores.csv and build the branch lookup data used by the app.

    Returns:
      code_to_name: branch code to friendly branch name
      alias_index: searchable aliases to branch code
      close_times: branch code to weekday/Saturday closing times
    """
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        # If the file is missing, the app can still run with branch codes only.
        return {}, {}, {}

    # Make the CSV header matching more forgiving.
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

    # Closing-time columns are optional.
    col_close_mf  = _pick(["Close_MF", "Close", "Closing", "Closing_MF", "MF_Close", "Weekday_Close", "MonFri_Close"])
    col_close_sat = _pick(["Close_Sat", "Sat_Close", "Saturday_Close", "Closing_Sat"])

    code_to_name: dict[str, str] = {}
    alias_index: dict[str, str] = {}
    close_times = {}

    for _, row in df.iterrows():
        # Build a canonical branch code like BR60, even if the CSV has BR 60 or just 60.
        code_raw = (row.get(col_code, "") or "").strip() if col_code else ""
        num_raw = (row.get(col_num, "") or "").strip() if col_num else ""
        name_raw = str(row.get(col_name, "")).strip() if col_name else ""
        # Prefer any branch number found in either field.
        digits = "".join(ch for ch in (code_raw + " " + num_raw) if ch.isdigit())
        code = ""
        if digits:
            code = f"BR{int(digits)}"
        else:
            # Fallback for values that look like BR plus a number.
            cr = "".join(ch for ch in code_raw.upper() if ch.isalnum())
            if cr.startswith("BR") and cr[2:].isdigit():
                code = f"BR{int(cr[2:])}"

        if not code:
            continue

        # Store close times if the CSV includes them.
        close_mf = parse_clock_time(row.get(col_close_mf)) if col_close_mf else None
        close_sat = parse_clock_time(row.get(col_close_sat)) if col_close_sat else None
        if close_mf or close_sat:
            close_times[code] = {"mf": close_mf, "sat": close_sat}

        # Friendly branch name, or just the code if no name exists.
        name = name_raw if name_raw else code
        code_to_name[code] = name

        # Build searchable aliases for the dropdowns.
        aliases = set()
        aliases.add(code)                              # BR60
        if code[2:].isdigit():
            aliases.add(code[2:])                      # 60
        # br60
        aliases.add(code[:2].lower() + code[2:])       # br60
        if name_raw:
            nm = name_raw.strip()
            aliases.add(nm)                            # "Merrill Company"
            aliases.add(nm.lower())                    # lowercase name
            # Also index a simpler version of the name.
            nm2 = nm.lower().replace(" company", "").strip()
            aliases.add(nm2)
            aliases.add(nm2.replace("the ", ""))

        # Add normalized aliases to the search index.
        for a in aliases:
            na = _norm(a)
            if not na:
                continue
            # Keep the first match if duplicate aliases exist.
            alias_index.setdefault(na, code)

    return code_to_name, alias_index, close_times

# --- Google sign-in helpers ---
def email_alias_key(value: str) -> str:
    """Normalize an email prefix or branch name so it can be matched to a branch."""
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
    """Use the signed-in company email to pick a default receiving branch when possible."""
    email_key = email_alias_key(email)
    if not email_key:
        return None

    # First check known email prefixes that do not match the branch name exactly.
    mapped_code = EMAIL_BRANCH_DEFAULTS.get(email_key)
    if mapped_code:
        mapped_code = canonical_br(mapped_code)
        if mapped_code in stops:
            return mapped_code

    # Then try to match against the branch name or code.
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
    """Check whether all required Streamlit Google auth secrets are available."""
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
    """Return the signed-in Google email address, if auth is configured and active."""
    try:
        if auth_is_configured() and getattr(st.user, "is_logged_in", False):
            return st.user.get("email")
    except Exception:
        pass
    return None


ALLOWED_GOOGLE_DOMAINS = {"arnoldgroupweb.com", "arnoldmotorsupply.com"}


def google_email_domain(email: str) -> str:
    """Return the domain portion of an email address."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[1]


def google_email_is_allowed(email: str) -> bool:
    """Return True when the email belongs to one of the allowed company domains."""
    return google_email_domain(email) in ALLOWED_GOOGLE_DOMAINS


def render_account_footer():
    """Show the Google sign-in/sign-out controls at the bottom of the app."""
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
    """Stop the app unless the user is signed in with an allowed company Google account."""
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
    Build search results for the branch picker.

    Empty search shows all branches. Typed search matches branch codes, numbers, names, and aliases.
    """
    def _branch_sort_key(code: str):
        c = canonical_br(code)
        if c.startswith("BR") and c[2:].isdigit():
            return (0, int(c[2:]))
        return (1, c)

    qn = _norm(query)

    # Empty search shows the full branch list.
    if not qn:
        ordered_stops = sorted(
            [s for s in stops if not exclude or s != exclude],
            key=_branch_sort_key,
        )
        return [
            (f"{display_br(code)} — {code_to_name.get(code, code)}", code)
            for code in ordered_stops
        ]

    # Exact alias/code matches should be shown first.
    raw = (query or "").strip()
    exact_code = alias_index.get(qn)

    # Also handle typed values like 60 or BR60.
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

    # Put obvious branch-number guesses first.
    guesses = []
    if query.strip().isdigit():
        guesses.append(f"BR{int(query.strip())}")
    elif query.strip().upper().startswith("BR") and query.strip()[2:].isdigit():
        guesses.append(f"BR{int(query.strip()[2:])}")

    seen = set()
    out  = []

    # Add valid branch-number guesses first.
    for g in guesses:
        if g in stops and (not exclude or g != exclude):
            label = f"{display_br(g)} — {code_to_name.get(g, g)}"
            out.append((label, g))
            seen.add(g)

    # Then search aliases, preferring starts-with matches.
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

    # Convert branch codes to dropdown labels.
    for code in ordered[: max(0, limit - len(out))]:
        label = f"{display_br(code)} — {code_to_name.get(code, code)}"
        out.append((label, code))

    return out

def display_name_for(code: str, code_to_name: dict[str, str]) -> str:
    """Return the friendly branch name when one is available."""
    return code_to_name.get(code, code)
def parse_hhmm(s):
    """
    Parse route schedule time cells into seconds since midnight.

    Handles normal time strings, datetime values, and timestamps. Unsupported formats return None.
    """
    if s is None or (isinstance(s, float) and pd.isna(s)) or (isinstance(s, str) and s.strip() == "") or pd.isna(s):
        return None

    # Handle real time/datetime values first.
    try:
        # datetime.time value
        if isinstance(s, time):
            return s.hour * 3600 + s.minute * 60 + s.second
    except Exception:
        pass

    try:
        # pandas Timestamp or Python datetime
        if isinstance(s, (pd.Timestamp, datetime)):
            t = s.time()
            return t.hour * 3600 + t.minute * 60 + t.second
    except Exception:
        pass

    try:
        # numpy datetime64
        import numpy as np
        if isinstance(s, np.datetime64):
            # Convert to pandas Timestamp, then extract the time.
            ts = pd.to_datetime(s)
            t = ts.time()
            return t.hour * 3600 + t.minute * 60 + t.second
    except Exception:
        pass

    # Handle strings that contain a time.
    try:
        s_str = str(s).strip()
        # Look for HH:MM or HH:MM:SS anywhere in the value.
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

    # Unsupported format.
    return None

def normalize_columns(df):
    # Make schedule column matching more forgiving.
    remap = {c.lower().replace(" ", "_"): c for c in df.columns}

    def _pick(cands):
        for c in cands:
            cc = remap.get(c.lower())
            if cc:
                return cc
        return None

    # Accept common variants for each expected schedule field.
    col_trip = _pick(["trip_id"]) or "Trip_ID"
    col_stop = _pick(["stop_id"]) or "Stop_ID"
    col_arr  = _pick(["arrival_time"]) or "Arrival_Time"
    col_dep  = _pick(["departure_time"]) or "Departure_Time"
    col_seq  = _pick(["sequence"]) or "Sequence"
    col_days = _pick(["days_active"]) or "Days_Active"
    col_method = _pick(["method", "delivery_method", "method_code"]) or "Method"  # optional

    # Build a clean DataFrame with the column names the rest of the app expects.
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
            # Missing columns get blank values so later code can safely ignore them.
            new_df[canonical] = pd.NA

    return new_df

def days_active_to_set(s):
    # Days are stored like "1,2,3,4,5" where Monday=1 and Sunday=7.
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
def read_all_connections(xlsx_path, file_mtime=None):
    """Read the route workbook and turn each trip into route legs the app can search."""
    xls = pd.ExcelFile(xlsx_path)
    connections = []  # each item is one searchable route leg
    all_stops = set()

    for sheet in xls.sheet_names:
        df = pd.read_excel(xls, sheet_name=sheet)
        if df.empty: 
            continue
        df = normalize_columns(df)
        # Some workbook sheets only list the Trip_ID once at the start of a route block.
        # Forward-fill it so the remaining stops stay grouped under the same trip.
        df["Trip_ID"] = df["Trip_ID"].replace(r"^\s*$", pd.NA, regex=True).ffill().fillna(sheet)

        # Parse times, stop order, and active days.
        df["dep_s"]  = df["Departure_Time"].apply(parse_hhmm)
        df["arr_s"]  = df["Arrival_Time"].apply(parse_hhmm)
        df["Sequence"] = pd.to_numeric(df["Sequence"], errors="coerce")
        df["days_set"] = df["Days_Active"].apply(days_active_to_set)

        # Keep only rows that can be ordered into a trip.
        df = df.dropna(subset=["Stop_ID","Sequence"]).sort_values(["Trip_ID","Sequence"])

        # Build route legs between consecutive stops in each trip.
        for trip_id, grp in df.groupby("Trip_ID"):
            g = grp.sort_values("Sequence")
            rows = g.to_dict("records")
            # Some night routes only give the first departure and final arrival.
            # When that happens, spread the time evenly across the stops in between.
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
                # Spread the total trip time evenly across each leg.
                dep0 = int(rows[0]["dep_s"])
                arrN = int(rows[-1]["arr_s"])
                # If the trip crosses midnight, push the arrival into the next day.
                if arrN < dep0:
                    arrN += 24 * 3600
                legs = len(rows) - 1  # number of route legs
                if legs > 0 and arrN > dep0:
                    segment = (arrN - dep0) / legs
                    for i in range(legs):
                        a = rows[i]
                        b = rows[i+1]
                        dep_s_raw = int(round(dep0 + i * segment))
                        arr_s_raw = int(round(dep0 + (i + 1) * segment))

                        # Track whether this leg happens after midnight.
                        dep_day_offset = dep_s_raw // (24 * 3600)
                        arr_day_offset = arr_s_raw // (24 * 3600)

                        # Store clock time plus the day offset.
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
                continue  # This trip has already been handled.

            # Regular trips have departure and arrival times on each leg.
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
    # Monday=1 through Sunday=7.
    return (dt_local.weekday() + 1)


# --- Store closing and next-open helpers ---
def store_close_time(code: str, dt_local: datetime, close_map: dict):
    """Return the closing time for a branch on the given local date."""
    code = canonical_br(code)
    info = close_map.get(code) or {}
    wd = weekday_num(dt_local)
    if wd == 6:  # Saturday.
        return info.get("sat")
    if 1 <= wd <= 5:  # Monday through Friday.
        return info.get("mf")
    return None


def next_business_open(dt_local: datetime, open_t: time = OPEN_TIME) -> datetime:
    """Return the next non-Sunday opening time after the provided datetime."""
    d = (dt_local + timedelta(days=1)).replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
    # Skip Sundays.
    for _ in range(7):
        if weekday_num(d) != 7:
            return d
        d = (d + timedelta(days=1)).replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
    return d

def expand_connections(conns, start_dt_local):
    """Expand repeating weekly route legs into absolute datetimes for the search window."""
    out = []
    horizon = start_dt_local + timedelta(hours=HOURS_LOOKAHEAD)
    d = start_dt_local.replace(hour=0, minute=0, second=0, microsecond=0)

    while d <= horizon:
        wd = weekday_num(d)
        for c in conns:
            if wd in c["days"]:
                dep_abs = d + timedelta(days=int(c.get("dep_day_offset", 0)), seconds=c["dep_s"])
                arr_abs = d + timedelta(days=int(c.get("arr_day_offset", 0)), seconds=c["arr_s"])

                # Safety check for routes that cross midnight.
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
    # Sort so equal departures prefer the quicker arrival.
    out.sort(key=lambda x: (x["dep"], x["arr"]))
    return out

def earliest_arrival(
    origin,
    dest,
    start_dt_local,
    abs_legs,
    transfer_sec=MIN_TRANSFER_SECONDS,
):
    """Find the earliest route from origin to destination using the available route legs."""
    if origin == dest:
        return start_dt_local, []

    best = {}           # stop -> earliest known arrival
    prev = {}           # stop -> previous stop and route leg
    best[origin] = start_dt_local

    for leg in abs_legs:
        u = leg["from"]
        v = leg["to"]
        dep = leg["dep"]
        arr = leg["arr"]

        if u in best:
            # Staying on the same trip does not need a transfer buffer.
            # Zero-dwell also helps the artificial legs created for night routes.
            same_trip_ok = False
            try:
                last_leg_to_u = prev[u][1]
                same_trip_ok = (last_leg_to_u["trip_id"] == leg["trip_id"])
            except Exception:
                same_trip_ok = False
            required_buffer = 0 if same_trip_ok else transfer_sec
            earliest_board = best[u] + timedelta(seconds=required_buffer)
            can_board = (earliest_board <= dep)

            # For true night orders, enforce the earliest usable next-day truck.
            # Weekend/daytime orders should simply wait for the next available route, even if that route is on a later date.
            # Route-specific rules win over origin-wide rules.
            if can_board and u == origin:
                try:
                    is_future_day_departure = dep.date() > start_dt_local.date()
                    is_night_order = start_dt_local.time() >= NIGHT_ORDER_CUTOFF
                    if is_future_day_departure and is_night_order:
                        # Use route-specific cutoff first, then fall back to the origin-wide cutoff.
                        min_dep_time = ROUTE_NEXTDAY_MIN_DEP.get((origin, dest)) or ORIGIN_NEXTDAY_MIN_DEP.get(origin)
                        if min_dep_time and dep.time() < min_dep_time:
                            can_board = False
                except Exception:
                    pass

            if can_board:
                if (v not in best) or (arr < best[v]):
                    best[v] = arr
                    prev[v] = (u, leg)

        # Stop once no later leg can improve the destination arrival.
        if dest in best and leg["dep"] > best[dest]:
            break

    if dest not in best:
        return None, None

    # Rebuild the chosen route.
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

# Validate a typed stop code or alias and suggest close matches.
def pick_stop(user_text: str, label: str, stops_list: list[str], alias_index: dict[str, str], code_to_name: dict[str, str]):
    raw = (user_text or "").strip()
    s_norm = _norm(raw)
    if not s_norm:
        st.error(f"Enter a {label}.")
        st.stop()

    # Let users type values like 30, BR30, or Merrill.
    resolved = alias_index.get(s_norm)
    if not resolved:
        # Try a simple BR-number guess if needed.
        if raw.isdigit():
            resolved = f"BR{int(raw)}"
        elif raw.upper().startswith("BR") and raw[2:].isdigit():
            resolved = f"BR{int(raw[2:])}"

    if resolved and resolved in stops_list:
        return resolved

    # If it still is not found, suggest close branch matches.
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
    # Starts-with matches are more useful than contains matches.
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

# ---------- App UI ----------

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


# Show the logo when the image file is available.
try:
    c1, c2, c3 = st.columns([1,3,1])
    with c2:
        st.image(LOGO_PATH, width='content')
except Exception:
    pass  # Keep the app running if the logo file is missing.

st.title("When Should it Arrive?")

# Require a company Google account before showing the route lookup.
require_allowed_google_account()


# Load the route schedule and branch directory.
try:
    schedule_mtime = os.path.getmtime(DATA_XLSX)
    conns, stops = read_all_connections(DATA_XLSX, schedule_mtime)

    # Load branch names, aliases, and closing times.
    code_to_name, alias_index, close_times = load_stores(STORES_CSV)

    # Include equivalent branches in the dropdown even when they are not schedule stops.
    stops_ui = sorted(
        s for s in (set(stops) | set(BR_EQUIV.keys()))
        if not is_internal_route_stop(s)
    )

    if not conns:
        st.error("No connections found. Check column names and that sheets contain Trip_ID, Stop_ID, Arrival_Time, Departure_Time, Sequence, Days_Active.")
        st.stop()
except Exception as e:
    st.exception(e)
    st.stop()


def branch_dropdown_options(stops: list[str], code_to_name: dict[str, str], exclude: str | None = None):
    """Build branch dropdown labels and a label-to-code lookup."""
    def _branch_sort_key(code: str):
        c = canonical_br(code)
        if c.startswith("BR") and c[2:].isdigit():
            return (0, int(c[2:]))
        return (1, c)

    ordered_stops = sorted(
        [
            s for s in stops
            if (not exclude or s != exclude)
            and not is_internal_route_stop(s)
        ],
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
    """Save the selected branch code so Streamlit reruns keep the selection."""
    selected_label = st.session_state.get(widget_key)
    if selected_label:
        selected_code = lookup.get(selected_label)
        if selected_code:
            st.session_state[state_key] = selected_code


# Use the signed-in company email to default the receiving branch when possible.
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

# Build the receiving list after supplier is selected so the same branch can be excluded.
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

# Clear the saved receiving branch if it now matches the supplier.
if dest and origin and dest == origin:
    st.session_state["selected_dest_code"] = None
    dest = None

# Make sure both branches are selected.
if not origin:
    st.warning("Pick an origin branch to see the ETA.")
    render_account_footer()
    st.stop()
if not dest:
    st.warning("Pick a destination branch to see the ETA.")
    render_account_footer()
    st.stop()

# Map equivalent branches to the actual routing node.
origin_node = route_node(origin)
dest_node = route_node(dest)

if origin == dest:
    st.error("Origin and destination cannot be the same.")
    st.stop()

# Also block equivalent branches, since they route as the same location.
if origin_node == dest_node:
    st.error("Origin and destination cannot be the same (some branches are routed as equivalents).")
    st.stop()

# Uncomment this if we ever want to show users when equivalent branches are remapped.
#if origin != origin_node or dest != dest_node:
#    st.info(
#        f"Note: routing uses {display_br(origin_node)} for {display_br(origin)} and {display_br(dest_node)} for {display_br(dest)}."
#    )


# Use a custom order time when selected; otherwise use the current time.
if st.session_state.get("custom_dt_active") and st.session_state.get("custom_dt_value"):
    start_dt = st.session_state["custom_dt_value"]
else:
    start_dt = datetime.now(TZ)

# If the supplier is closed at the order time, start routing at the next business opening.
routing_start_dt = start_dt
origin_close_t = store_close_time(origin_node, routing_start_dt, close_times)
if origin_close_t:
    close_dt_today = routing_start_dt.replace(hour=origin_close_t.hour, minute=origin_close_t.minute, second=0, microsecond=0)
    if routing_start_dt > close_dt_today:
        routing_start_dt = next_business_open(routing_start_dt, open_t=OPEN_TIME)
        # No warning is shown; the ETA simply starts from the next open time.

# --- Calculate ETA automatically ---
# Final safety check for same/equivalent branches.
if origin_node == dest_node:
    st.error("Origin and destination cannot be the same. Please choose a different destination branch.")
    st.stop()


abs_legs = expand_connections(conns, routing_start_dt)


# --- BR30 gateway rule ---
# Freight from BR30 into the BR60/BR83 network must use one of the approved gateway paths:
# - LM through BR34
# - NT through BR81 for the new BR30/BR60 night meetup
# This prevents the app from choosing unrelated BR30 night routes while allowing the approved BR81 meetup.

def _m(x):
    return (x.get("method") or "").strip().upper()

def _is_blank_method(x) -> bool:
    return _m(x) == ""

def _passes_through(nodes_steps, node_code: str) -> bool:
    return any((l.get("from") == node_code or l.get("to") == node_code) for l in (nodes_steps or []))

def _first_touch_index(nodes_steps, node_code: str):
    """Return the first route-leg index where a node appears, or None if it is not in the route."""
    for i, l in enumerate(nodes_steps or []):
        if l.get("from") == node_code or l.get("to") == node_code:
            return i
    return None


def _touches_before(nodes_steps, first: str, later_nodes: tuple[str, ...]) -> bool:
    """Return True when one node is reached before any of the later nodes."""
    i_first = _first_touch_index(nodes_steps, first)
    if i_first is None:
        return False
    idxs = [_first_touch_index(nodes_steps, n) for n in later_nodes]
    idxs = [i for i in idxs if i is not None]
    return bool(idxs) and i_first < min(idxs)


def _has_direct_leg(nodes_steps, from_node: str, to_node: str) -> bool:
    """Return True when the selected route includes a direct leg between two nodes."""
    return any(
        l.get("from") == from_node and l.get("to") == to_node
        for l in (nodes_steps or [])
    )

# First try the normal fastest route.
eta, steps = earliest_arrival(
    origin_node,
    dest_node,
    routing_start_dt,
    abs_legs,
    transfer_sec=MIN_TRANSFER_SECONDS,
)



if steps and any(l.get("from") == "BR30" for l in steps) and _touches_before(steps, "BR30", ("BR60", "BR83")):
    # Allow either the old LM gateway through BR34 or the new NT gateway through BR81.
    allowed_trip_ids = set()

    for leg in abs_legs:
        if leg.get("from") != "BR30":
            continue

        tid = leg.get("trip_id")
        method = _m(leg)

        # Old allowed path: BR30 LM/blank-method trip that touches BR34.
        if method == BR30_BR60_LM_GATEWAY_METHOD or _is_blank_method(leg):
            if any(
                other_leg.get("trip_id") == tid
                and (
                    other_leg.get("from") == BR30_BR60_LM_GATEWAY_STOP
                    or other_leg.get("to") == BR30_BR60_LM_GATEWAY_STOP
                )
                for other_leg in abs_legs
            ):
                allowed_trip_ids.add(tid)

        # New allowed path: BR30 NT trip that touches BR81.
        if method == BR30_BR60_NT_GATEWAY_METHOD:
            if any(
                other_leg.get("trip_id") == tid
                and (
                    other_leg.get("from") == BR30_BR60_NT_GATEWAY_STOP
                    or other_leg.get("to") == BR30_BR60_NT_GATEWAY_STOP
                )
                for other_leg in abs_legs
            ):
                allowed_trip_ids.add(tid)

    # If no gateway shuttle is found, stop with a clear schedule-data message.
    if not allowed_trip_ids:
        st.error(
            "BR30→BR60/BR83 freight must leave BR30 on either the LM shuttle through BR34 or the NT route through BR81, "
            "but no approved gateway trip was found in the current schedule window. "
            "Check RouteSchedule.xlsx for either a BR30 LM trip that reaches BR34 or a BR30 NT trip that reaches BR81 on the appropriate day(s)/time(s)."
        )
        st.stop()

    # Enforce the rule by blocking any BR30 departure that is not on an allowed gateway trip.
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
            "BR30→BR60/BR83 freight found an approved gateway trip, "
            "but no feasible route was found from that gateway to the destination within the lookahead window. "
            "Double-check that the BR30 gateway route connects onward to BR60, and then to the final destination, on the correct day(s)."
        )
        st.stop()

    # Use the constrained path when the gateway rule applies.
    eta, steps = eta2, steps2


# Decide what time to show to the user as the ready/arrival time.
# Early-morning arrivals show as ready at opening time.
# If the same trip leaves the destination shortly after arriving, use that departure as the ready time.
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

    # Suggest the delivery method for DC origins.
    # Start with the first origin leg, but prefer NT when the chosen route is truly overnight.
    delivery_hint = None
    if steps and origin_node in DC_ORIGINS:
        # Prefer the first leg that actually leaves the selected origin DC.
        origin_legs = [leg for leg in steps if leg.get("from") == origin_node]
        if origin_legs:
            first_leg = origin_legs[0]
        else:
            # Fallback to the first leg in the route.
            first_leg = steps[0]

        method_code = (first_leg.get("method") or "").strip().upper()

        if method_code:
            # For true overnight deliveries, only prefer NT when the first origin leg is NT.
            # If BR60 leaves on SM and later connects to an NT leg, the store should still submit as SM.
            step_methods = [(l.get("method") or "").strip().upper() for l in steps]
            if method_code == "NT":
                # Final overnight check happens after overnight_msg is calculated.
                preferred_overnight_method = "NT"
            else:
                preferred_overnight_method = None

            # Delivery-method business rule overrides.
            passes_through_br60 = any((l.get("from") == "BR60" or l.get("to") == "BR60") for l in steps)

            # BR51 routes that hand off to BR60 should be ordered as SHU.
            if origin_node == "BR51" and passes_through_br60:
                method_code = "SHU"
                preferred_overnight_method = None

            
            # BR30 routes that pass through BR60 should show the method from the actual BR30 departure.
            # Older BR30→BR60 routes use LM through BR34, while the new BR81 meetup uses NT.
            if origin_node == "BR30" and passes_through_br60:
                br30_origin_legs = [leg for leg in steps if leg.get("from") == "BR30"]
                br30_first_method = ""
                if br30_origin_legs:
                    br30_first_method = (br30_origin_legs[0].get("method") or "").strip().upper()

                if br30_first_method:
                    method_code = br30_first_method

                if method_code != "NT":
                    preferred_overnight_method = None

            # Save the base method now; finalize it after the overnight message is known.
            delivery_hint = {
                "base_method": method_code,
                "preferred_overnight": preferred_overnight_method,
            }
    # If the raw arrival lands overnight, show it as ready when the store opens.
    overnight_msg = False
    opening_dt_for_msg = None
    if steps:
        last_leg = steps[-1]
        dest_arr = last_leg["arr"]
        t = dest_arr.time()
        # Overnight means 18:01–23:59 or 00:00–06:59.
        arrives_evening = (t.hour > 18) or (t.hour == 18 and t.minute >= 1)
        arrives_early   = (t.hour < 7)    # 00:00–06:59
        if arrives_evening or arrives_early:
            overnight_msg = True
            if arrives_evening:
                # Evening arrivals are ready at next-day opening.
                opening_dt_for_msg = (dest_arr + timedelta(days=1)).replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute, second=0, microsecond=0)
            else:
                # Early-morning arrivals are ready at same-day opening.
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

    # Finalize the delivery-method hint now that overnight status is known.
    if isinstance(delivery_hint, dict):
        base_method = delivery_hint.get("base_method")
        preferred_overnight_method = delivery_hint.get("preferred_overnight")
        method_to_show = base_method

        # Overnight routes with an NT leg should recommend NT.
        if overnight_msg and preferred_overnight_method:
            method_to_show = preferred_overnight_method

        if method_to_show:
            delivery_hint = (
                f"To get this ETA, submit your order from {origin_name} using "
                f"delivery method <b>{method_to_show}</b>."
            )
        else:
            delivery_hint = None

    # Show the latest order time that still catches the first leg.
    if steps:
        first_leg = steps[0]
        first_method = (first_leg.get("method") or "").strip().upper()
        cutoff_dt = first_leg["dep"] - timedelta(seconds=MIN_TRANSFER_SECONDS)

        # Store-origin night-truck pickups should use the store closing time as the order cutoff,
        # not the time the driver gets there. DC origins still use the normal route cutoff logic.
        close_t = store_close_time(origin_node, first_leg["dep"], close_times)
        if close_t:
            close_dt = first_leg["dep"].replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)

            if first_method == "NT" and origin_node not in DC_ORIGINS:
                # If the night-truck pickup time is after midnight, the cutoff belongs to the prior business day.
                # Example: a 12:30 AM Wednesday pickup should use Tuesday's close-minus-buffer cutoff.
                if close_dt > first_leg["dep"]:
                    close_dt = close_dt - timedelta(days=1)
                cutoff_dt = close_dt - timedelta(seconds=MIN_TRANSFER_SECONDS)
            elif close_dt < cutoff_dt:
                cutoff_dt = close_dt

        # Format the cutoff for display.
        cutoff_display = cutoff_dt.strftime('%a %B %d, %Y %I:%M %p')
        st.markdown(
            f"<div class='order-cutoff'>Order by <b>{cutoff_display}</b> to receive by this ETA.</div>",
            unsafe_allow_html=True,
        )

    if delivery_hint:
        st.markdown(f"<div class='method-note'>{delivery_hint}</div>", unsafe_allow_html=True)

    st.caption(f"(Note: This order ETA is an ESTIMATE only. Actual arrival could change depending on unforeseen circumstances.)")
    st.markdown("---")


    # --- Optional detail toggles ---
    # Keep these buttons full width in their columns.
    st.markdown("<style>.linklike > button{width:100%;}</style>", unsafe_allow_html=True)

    # Current toggle states.
    show_route = st.session_state.get("show_route_open", False)
    show_custom = st.session_state.get("show_custom_dt_open", False)

    # Show the optional-detail buttons side by side.
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

    # --- Route timeline ---
    if steps and st.session_state.get("show_route_open", False):
        route_col, handoff_col = st.columns([2, 1])

        with route_col:
            parts = [
                "<div class='timeline'>",
                "<div class='timeline-item'>",
                "<div class='timeline-title'>Truck routes</div>",
                "</div>",
            ]
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

        with handoff_col:
            handoff_parts = [
                "<div class='timeline'>",
                "<div class='timeline-item'>",
                "<div class='timeline-title'>Handoff summary</div>",
                "</div>",
            ]

            # Start with the supplier branch and the time the part leaves.
            first_leg = steps[0]
            origin_departure_txt = first_leg["dep"].strftime("%a %b %d, %Y %I:%M %p")
            handoff_parts.append(
                f"<div class='timeline-item'>"
                f"<div class='timeline-title'>{display_br(first_leg['from'])} — {display_name_for(first_leg['from'], code_to_name)}</div>"
                f"<div class='timeline-meta'>Leaves: {origin_departure_txt}</div>"
                f"</div>"
            )

            # Add only the places where the part switches from one truck/trip to another.
            for i in range(len(steps) - 1):
                current_leg = steps[i]
                next_leg = steps[i + 1]

                current_method = (current_leg.get("method") or "").strip().upper()
                next_method = (next_leg.get("method") or "").strip().upper()

                same_stop = current_leg.get("to") == next_leg.get("from")
                trip_changed = current_leg.get("trip_id") != next_leg.get("trip_id")
                method_changed = current_method != next_method

                if same_stop and (trip_changed or method_changed):
                    stop_code = current_leg["to"]
                    stop_name = display_name_for(stop_code, code_to_name)
                    arrival_txt = current_leg["arr"].strftime("%a %b %d, %Y %I:%M %p")

                    handoff_parts.append(
                        f"<div class='timeline-item'>"
                        f"<div class='timeline-title'>{display_br(stop_code)} — {stop_name}</div>"
                        f"<div class='timeline-meta'>Arrives: {arrival_txt}</div>"
                        f"</div>"
                    )

            # End with the receiving branch and the shown ETA/ready time.
            final_arrival_dt = eta_display or steps[-1]["arr"]
            final_arrival_txt = final_arrival_dt.strftime("%a %b %d, %Y %I:%M %p")
            handoff_parts.append(
                f"<div class='timeline-item'>"
                f"<div class='timeline-title'>{display_br(dest)} — {display_name_for(dest, code_to_name)}</div>"
                f"<div class='timeline-meta'>Arrives: {final_arrival_txt}</div>"
                f"</div>"
            )

            handoff_parts.append("</div>")
            st.markdown("".join(handoff_parts), unsafe_allow_html=True)

    # --- Custom order date/time chooser ---
    if st.session_state.get("show_custom_dt_open", False):
        _now_local = datetime.now(TZ)
        # If the saved value is empty, fall back to now.
        active_dt = st.session_state.get("custom_dt_value") or _now_local
        default_date = active_dt.date()
        default_time = active_dt.time().replace(second=0, microsecond=0)

        test_date = st.date_input("Order date", value=default_date, key="order_date")
        # Use a 12-hour time input because it is easier for store users.
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
                # Clear the saved custom time; the UI will fall back to now.
                st.session_state["custom_dt_value"] = None
                st.rerun()

    # Keep account controls at the bottom so they do not interrupt the route lookup.
    render_account_footer()

    # No extra handling needed here; same-branch routes are blocked above.
    pass

