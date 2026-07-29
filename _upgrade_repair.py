"""Upgrade RepairScheduler to support IssueGraph integration."""
import sys

PATH = r"pdf2zh/v3/evaluator.py"

with open(PATH, encoding="utf-8") as f:
    content = f.read()

old_class = '''class RepairScheduler:
    """Schedules repair tasks based on issues found."""
    def __init__(self):
        self._repairs = []
    def schedule(self, issue):
        repair = {"issue_type": issue.issue_type, "node_id": issue.node_id, "module": issue.module,
                  "action": {"overlap": "relayout", "bad_translation": "retranslate",
                             "missing_node": "retranslate", "empty_text": "retranslate",
                             "term_inconsistency": "retranslate", "font_mismatch": "reformat",
                             "overflow": "relayout"}.get(issue.issue_type, "reinspect"),
                  "priority": {"critical": 1, "major": 2, "minor": 3, "info": 4}.get(issue.severity.value, 5)}
        self._repairs.append(repair)
        return repair
    def schedule_all(self, issues):
        for m in issues.modules:
            for i in issues.get_by_module(m): self.schedule(i)
        return self.list_repairs()
    def list_repairs(self): return list(self._repairs)
    def clear(self): self._repairs.clear()'''

new_class = '''class RepairScheduler:
    """Schedules repair tasks based on issues found.

    Can optionally be bound to an IssueGraph for automatic repair scheduling.
    """
    def __init__(self, issue_graph=None):
        self._repairs = []
        self._issue_graph = issue_graph

    def bind_issue_graph(self, issue_graph):
        self._issue_graph = issue_graph

    def schedule(self, issue):
        repair = {"issue_type": issue.issue_type, "node_id": issue.node_id, "module": issue.module,
                  "action": {"overlap": "relayout", "bad_translation": "retranslate",
                             "missing_node": "retranslate", "empty_text": "retranslate",
                             "term_inconsistency": "retranslate", "font_mismatch": "reformat",
                             "overflow": "relayout"}.get(issue.issue_type, "reinspect"),
                  "priority": {"critical": 1, "major": 2, "minor": 3, "info": 4}.get(issue.severity.value, 5)}
        self._repairs.append(repair)
        return repair

    def schedule_all(self, issues):
        if isinstance(issues, IssueGraph):
            for m in issues.modules:
                for i in issues.get_by_module(m):
                    self.schedule(i)
        elif hasattr(issues, "__iter__"):
            for i in issues:
                self.schedule(i)
        return self.list_repairs()

    def schedule_from_issues(self):
        """Convenience: schedule from bound IssueGraph."""
        if self._issue_graph:
            return self.schedule_all(self._issue_graph)
        return []

    def list_repairs(self):
        return list(self._repairs)

    def clear(self):
        self._repairs.clear()

    def execute_all(self, graph=None):
        """Execute all scheduled repairs on a DocumentGraph."""
        executed = []
        for repair in self._repairs:
            action = repair.get("action", "reinspect")
            if action == "retranslate" and graph:
                node = graph.get_node(repair["node_id"])
                if node:
                    node.translated_text = None
                    node.confidence = 0.0
                    executed.append(repair)
            elif action in ("relayout", "reformat", "reinspect"):
                executed.append(repair)
        return executed'''

if old_class in content:
    content = content.replace(old_class, new_class)
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print("RepairScheduler upgraded successfully")
else:
    print("Old class not found - checking for exact match...")
    # Debug: find the class
    idx = content.find("class RepairScheduler")
    if idx >= 0:
        print("Found at position", idx)
        print(content[idx:idx+800])
