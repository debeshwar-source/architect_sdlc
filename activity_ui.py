"""The Activity card: what the agents are doing, shown like a terminal.

Plain words: a quiet card, closed unless you open it. Open it before an agent starts and the lines appear as they happen; open it later and you
see what happened. Pick one agent, or all, and how many lines. It only reads the log (activity.py); it changes nothing.
"""

import streamlit as st

import activity

CSS = """
<style>
[class*="st-key-act_term"] pre, [class*="st-key-act_term"] code{background:#0d1117 !important;color:#c9d1d9 !important;font-size:.78rem}
</style>
"""
ALL = "All agents"
SIZES = [25, 100, 300]


@st.fragment(run_every="3s")
def _terminal(who: str, size: int) -> None:
    """The lines. Refreshes by itself every few seconds while the page is idle; while an agent is working the running code redraws it instead
    (the page cannot refresh itself while it is busy)."""
    box = st.empty()

    def draw() -> None:
        lines = [activity.format_line(e) for e in activity.recent(size, None if who == ALL else who)]
        box.code("\n".join(lines) or "Nothing yet. Agent activity shows here as it happens.", language="text", height="content" if size <= 25 else 420)
    draw()
    activity.attach(draw)


def render() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    left, mid, right = st.columns([3, 2, 1])
    options = [ALL] + activity.agents()
    who = left.selectbox("Agent", options, key="act_agent", label_visibility="collapsed")
    size = mid.selectbox("Lines", SIZES, key="act_lines", label_visibility="collapsed", format_func=lambda n: f"last {n} lines")
    if right.button("Clear", key="act_clear"):
        activity.clear()
        st.rerun()
    with st.container(key="act_term"):
        _terminal(who if who in options else ALL, size)
