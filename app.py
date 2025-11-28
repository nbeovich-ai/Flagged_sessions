import io
import pandas as pd
import streamlit as st


st.set_page_config(page_title="BT Payment Triage", layout="wide")


def normalize_columns(df: pd.DataFrame):
    """Normalize headers to ASCII snake_case so matching is reliable."""
    clean = (
        df.columns
        .str.normalize("NFKD")
        .str.encode("ascii", errors="ignore")
        .str.decode("ascii")
        .str.lower()
        .str.replace(r"[^0-9a-z]+", "_", regex=True)
        .str.strip("_")
    )
    rename = dict(zip(df.columns, clean))
    return df.rename(columns=rename), rename


def derive_reasons(row: pd.Series) -> str:
    """Explain why a session is not ready or paid based on common flags."""
    val = lambda col: str(row.get(col, "") or "").strip()
    reasons = []

    if val("payroll_process").lower() in ("false", "0", "no"):
        reasons.append("Payroll process flag is FALSE")
    if "missing gps" in val("location_status").lower():
        reasons.append("Missing GPS")
    if val("hours_status").lower().startswith("pending"):
        reasons.append(f"Hours status {val('hours_status')}")
    if val("overall_status").lower().startswith("pending"):
        reasons.append(f"Overall status {val('overall_status')}")

    delta = val("delta_vs_billing_hh_mm_ss")
    if delta not in ("", "0:00:00", "0:0:0", "0"):
        reasons.append(f"Delta vs billing {delta}")

    sig = val("parent_s_signature_approval_for_time_adjustment_signature")
    sig_time = val("parent_s_signature_approval_for_time_adjustment_signature_time")
    if sig and not sig_time:
        reasons.append("Parent signature present but missing time stamp")

    aloha = val("aloha_status")
    if aloha and "ready" not in aloha.lower():
        reasons.append(f"Aloha status {aloha}")

    return ", ".join(reasons) if reasons else "Ready to bill"


def row_reason_from_notes_or_hours(row: pd.Series) -> str:
    """Preferred reason: other_notes if present, otherwise hours_status."""
    def clean(val):
        if pd.isna(val):
            return ""
        return str(val).strip()

    other = clean(row.get("other_notes", ""))
    if other:
        return other
    hours = clean(row.get("hours_status", ""))
    return hours or "No reason provided"


def is_unverified(row: pd.Series) -> bool:
    """Flag rows as unverified/cancelled based on aloha_status."""
    val = row.get("aloha_status", "")
    v = "" if pd.isna(val) else str(val).lower()
    return any(term in v for term in ["unverified", "cancelled"])


def main():
    st.title("BT Payment Triage")
    st.write("Reading from Google Sheets (All_flagged_sessions) to find why a BT was not paid.")

    st.caption(
        "Set a public or authorized CSV export URL for the Google Sheet in Streamlit secrets as GSHEET_CSV_URL. "
        "Optionally paste a URL below for testing."
    )

    default_sheet_url = st.secrets.get("GSHEET_CSV_URL", "")
    sheet_url = st.text_input("Google Sheet CSV export URL", value=default_sheet_url, placeholder="https://docs.google.com/spreadsheets/d/.../export?format=csv")

    if not sheet_url:
        st.error("Please provide a Google Sheets CSV export URL (set GSHEET_CSV_URL in secrets or paste above).")
        st.stop()

    @st.cache_data(ttl=60)
    def load_data(url: str) -> pd.DataFrame:
        return pd.read_csv(url)

    if st.button("Refresh data now"):
        load_data.clear()

    try:
        df_raw = load_data(sheet_url)
    except Exception as e:
        st.error(f"Failed to load Google Sheet: {e}")
        st.stop()

    df, rename_map = normalize_columns(df_raw)

    # Parse dates if present
    if "appt_date" in df.columns:
        df["appt_date"] = pd.to_datetime(df["appt_date"], errors="coerce")

    # Numeric minutes and deltas
    if "scheduled_minutes" in df.columns:
        df["scheduled_minutes"] = pd.to_numeric(df["scheduled_minutes"], errors="coerce")
    if "actual_minutes" in df.columns:
        df["actual_minutes"] = pd.to_numeric(df["actual_minutes"], errors="coerce")
    if "scheduled_minutes" in df.columns and "actual_minutes" in df.columns:
        df["difference_in_minutes"] = df["actual_minutes"] - df["scheduled_minutes"]

    # Derive reasons
    df["not_paid_reason"] = df.apply(derive_reasons, axis=1)
    df["unverified"] = df.apply(is_unverified, axis=1)
    df["unverified_reason"] = df.apply(row_reason_from_notes_or_hours, axis=1)
    df["reason_session_unverified"] = df["unverified_reason"]

    # Unverified view with fuzzy search in tabs
    st.subheader("Unverified sessions")
    st.caption("Rows where aloha_status contains 'unverified' or 'cancelled'.")

    tab_detail, tab_compact = st.tabs(["Detail view", "Compact view"])

    keyword = st.text_input("Fuzzy search by staff name, phone, or client", "")
    unverified_df = df[df["unverified"]].copy()

    if keyword:
        key = keyword.lower().strip()
        search_cols = [c for c in ("staff_name", "phone", "client") if c in unverified_df.columns]
        if not search_cols:
            st.warning("No staff_name or phone columns found for search.")
            search_cols = unverified_df.columns.tolist()

        def row_matches(row):
            text = " | ".join([str(row.get(c, "")) for c in search_cols]).lower()
            if key in text:
                return True
            from difflib import SequenceMatcher

            for col in search_cols:
                val = str(row.get(col, ""))
                v = val.lower()
                if not v:
                    continue
                ratio = SequenceMatcher(None, key, v).ratio()
                if ratio >= 0.6:
                    return True
            return False

        unverified_df = unverified_df[unverified_df.apply(row_matches, axis=1)]

    with tab_detail:
        display_cols = [
            "staff_name",
            "phone",
            "client",
            "appt_date",
            "service_name",
            "aloha_status",
            "unverified_reason",
            "scheduled_minutes",
            "actual_minutes",
            "difference_in_minutes",
            "other_notes",
            "hours_status",
        ]
        display_cols = [c for c in display_cols if c in unverified_df.columns]

        st.dataframe(unverified_df[display_cols], use_container_width=True, height=400)

        # Download unverified subset
        csv_buf = io.StringIO()
        unverified_df.to_csv(csv_buf, index=False)
        st.download_button(
            "Download unverified CSV",
            csv_buf.getvalue(),
            file_name="unverified_sessions.csv",
            mime="text/csv",
        )

    with tab_compact:
        compact_cols = [
            "staff_name",
            "phone",
            "client",
            "reason_session_unverified",
            "scheduled_minutes",
            "actual_minutes",
            "difference_in_minutes",
        ]
        compact_cols = [c for c in compact_cols if c in unverified_df.columns]
        st.dataframe(unverified_df[compact_cols], use_container_width=True, height=400)


if __name__ == "__main__":
    main()
