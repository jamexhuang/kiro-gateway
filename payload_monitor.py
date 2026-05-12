import os
import json
import time
from datetime import datetime
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Tree, Label, ListView, ListItem
from textual.containers import Horizontal, Vertical
from textual.worker import get_current_worker

FILEPATH = os.path.expanduser("~/Documents/github/kiro-gateway/debug_logs/kiro_request_body.json")

class PayloadMonitorApp(App):
    """An interactive TUI for monitoring Kiro Gateway payloads."""
    
    CSS = """
    Screen {
        layout: horizontal;
    }
    #left-pane {
        width: 30%;
        border-right: solid green;
    }
    #right-pane {
        width: 70%;
        padding: 1;
    }
    ListView {
        height: 100%;
        border: none;
    }
    Tree {
        height: 100%;
        border: none;
    }
    ListView:focus {
        background: $boost;
    }
    Tree:focus {
        background: $boost;
    }
    ListItem {
        padding: 1;
        border-bottom: solid $primary-background;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("c", "clear_log", "Clear History"),
        ("tab", "focus_next", "Switch Pane"),
    ]

    def __init__(self):
        super().__init__()
        self.last_mtime = 0
        self.payloads_data = [] # Store actual payload dicts

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left-pane"):
                yield Label(" 📡 Request History", classes="pane-header")
                yield ListView(id="request_list")
            with Vertical(id="right-pane"):
                yield Label(" 🔍 Payload Details", classes="pane-header")
                tree = Tree("Select a payload from the left...")
                yield tree
        yield Footer()

    def on_mount(self) -> None:
        self.list_widget = self.query_one("#request_list", ListView)
        self.tree_widget = self.query_one(Tree)
        self.run_worker(self.monitor_file, exclusive=True, thread=True)

    def action_clear_log(self) -> None:
        self.list_widget.clear()
        self.payloads_data.clear()
        self.tree_widget.clear()
        self.tree_widget.root.label = "Waiting for new payload..."

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # 當左側清單被按下 Enter 選擇時，才更新右側的樹狀圖
        index = self.list_widget.index
        if index is not None and 0 <= index < len(self.payloads_data):
            data_dict, timestamp = self.payloads_data[index]
            self.update_tree(data_dict, timestamp)

    def build_tree_node(self, node, data):
        """Recursively build the tree from dict/list data."""
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    preview = f"[{len(value)} items]" if isinstance(value, list) else "{...}"
                    child = node.add(f"[b]{key}[/b]: {preview}")
                    self.build_tree_node(child, value)
                else:
                    val_str = str(value)
                    if len(val_str) > 200:
                        val_str = val_str[:100] + " ... [Truncated] ... " + val_str[-100:]
                    node.add_leaf(f"[b]{key}[/b]: [green]{val_str}[/green]")
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, (dict, list)):
                    preview = "Object" if isinstance(item, dict) else "Array"
                    child = node.add(f"[b][{i}][/b]: {preview}")
                    self.build_tree_node(child, item)
                else:
                    val_str = str(item)
                    if len(val_str) > 200:
                        val_str = val_str[:100] + " ... [Truncated] ... " + val_str[-100:]
                    node.add_leaf(f"[b][{i}][/b]: [green]{val_str}[/green]")

    def update_tree(self, data: dict, timestamp: str) -> None:
        self.tree_widget.clear()
        
        current_msg = data.get('conversationState', {}).get('currentMessage', {}).get('userInputMessage', {})
        model = current_msg.get('modelId', 'unknown')
        tools = current_msg.get('userInputMessageContext', {}).get('tools', [])
        history = data.get('conversationState', {}).get('history', [])

        self.tree_widget.root.label = f"🚀 Payload @ {timestamp} | Model: [yellow]{model}[/yellow] | Tools: {len(tools)} | History: {len(history)}"
        
        self.build_tree_node(self.tree_widget.root, data)
        self.tree_widget.root.expand()

        for child in self.tree_widget.root.children:
            if "conversationState" in str(child.label):
                child.expand()
                for subchild in child.children:
                    if "currentMessage" in str(subchild.label):
                        subchild.expand()
                        for subsub in subchild.children:
                            if "userInputMessage" in str(subsub.label):
                                subsub.expand()

    def add_to_list(self, data: dict, timestamp: str) -> None:
        self.payloads_data.append((data, timestamp))
        
        current_msg = data.get('conversationState', {}).get('currentMessage', {}).get('userInputMessage', {})
        content = current_msg.get('content', '(empty)')
        if len(content) > 30:
            content = content[:27] + "..."
            
        tools = current_msg.get('userInputMessageContext', {}).get('tools', [])
        history = data.get('conversationState', {}).get('history', [])

        label_text = f"[bold green]{timestamp}[/]\nT:{len(tools)} H:{len(history)}\n[dim]{content}[/dim]"
        self.list_widget.append(ListItem(Label(label_text)))
        self.list_widget.scroll_end(animate=False)
        
        # 只有在第一筆資料進來時，才自動更新並對焦到右邊
        if len(self.payloads_data) == 1:
            self.update_tree(data, timestamp)
            self.tree_widget.focus()

    def process_new_payload(self, data: dict) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.call_from_thread(self.add_to_list, data, timestamp)

    def monitor_file(self) -> None:
        worker = get_current_worker()
        while not worker.is_cancelled:
            try:
                if os.path.exists(FILEPATH):
                    mtime = os.path.getmtime(FILEPATH)
                    if mtime > self.last_mtime:
                        self.last_mtime = mtime
                        time.sleep(0.1)
                        with open(FILEPATH, 'r') as f:
                            content = f.read()
                            if content.strip():
                                data = json.loads(content)
                                self.process_new_payload(data)
            except json.JSONDecodeError:
                pass
            except Exception:
                pass
            time.sleep(0.5)

if __name__ == "__main__":
    app = PayloadMonitorApp()
    app.run()
