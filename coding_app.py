"""Web UI for the coding agent on its own. Run: streamlit run coding_app.py

The same screen is also inside the main app (streamlit_app.py), under the release plan of a session.
Each part is a small dropdown that opens only when you open it; the last one reviews the pull requests the coding agent raised (review/).
"""

import streamlit as st

from coding import tasks as task_list
from coding import ui
import activity
import activity_ui
from qa import ui as qa_ui
from review import ui as review_ui
from ui_kit import dropdown

st.set_page_config(page_title="Manager 2 and Coding Agent", page_icon="🛠️", layout="wide")
st.title("Engineering Manager 2 → Coding Agent")
activity.detach()
dropdown("🖥️ Activity", "dd_activity", activity_ui.render, lazy=True)       # before the agents, so what they do appears in it as it happens
ui.render(task_list.list_tasks(), sidebar=False)
dropdown("🔍 Code reviewer", "dd_review", lambda: review_ui.render(), lazy=True)
dropdown("🧪 QA agent", "dd_qa", lambda: qa_ui.render(), lazy=True)
