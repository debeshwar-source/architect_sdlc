"""Web UI for the code reviewer on its own. Run: streamlit run review_app.py

The same screen is also a tab in coding_app.py, next to the coding agent whose pull requests it reviews.
"""

import streamlit as st

from review import ui

st.set_page_config(page_title="Code Review", page_icon="🔍", layout="wide")
st.title("Code review of pull requests")
ui.render()
