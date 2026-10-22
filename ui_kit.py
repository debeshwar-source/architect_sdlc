"""Small screen pieces shared by the app's pages.

Plain words: `dropdown` is a card header (▸ closed, ▾ open) that shows a whole section only when the user asks for it.
Streamlit's own expander cannot hold another expander, and these sections are full of them, so this is drawn from a button instead.
"""

from typing import Callable

import streamlit as st

# Every card header is a button whose key contains "dd_" and ends "_head": styled as a quiet full-width card.
CSS = """
<style>
[class*="dd_"][class*="_head"] button{width:100%;justify-content:flex-start;text-align:left;font-weight:600;padding:.7rem 1rem;
  border-radius:12px;border:1px solid rgba(128,128,128,.28);background:rgba(128,128,128,.05);transition:all .15s ease}
[class*="dd_"][class*="_head"] button:hover{border-color:#6366f1;background:rgba(99,102,241,.09)}
[class*="dd_"][class*="_head"]{margin-bottom:-.35rem}
[class*="dd_"][class*="_body"]{border-radius:12px}
</style>
"""


def _flip(key: str) -> None:
    st.session_state[key] = not st.session_state.get(key, False)


def dropdown(label: str, key: str, body: Callable[[], None], lazy: bool = False) -> bool:
    """A closed-by-default section. Returns True when it is open.

    lazy=False: the section is always drawn but hidden while closed, so what the person typed or picked inside it is kept (Streamlit
                forgets the value of any widget that is not drawn in a run).
    lazy=True:  the section is not drawn at all while closed, so nothing inside it runs (use when drawing it costs a network call)."""
    opened = bool(st.session_state.get(key, False))
    st.markdown(CSS, unsafe_allow_html=True)
    st.button(f"{'▾' if opened else '▸'}  {label}", key=key + "_head", on_click=_flip, args=(key,), use_container_width=True)
    if lazy and not opened:
        return False
    with st.container(key=key + "_body", border=True):
        body()
    if not opened:
        st.markdown(f"<style>.st-key-{key}_body{{display:none}}</style>", unsafe_allow_html=True)
    return opened
