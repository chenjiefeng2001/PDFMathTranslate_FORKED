"""Module: InspectorView — Phase D8 文档 Inspector（DevTools 式单文件 GUI）。

输出**自包含 HTML**：左树（NodeID 层级）/ 中 SVG Overlay（角色着色）/
右侧板（生命周期 + 字段 + 决策证据 + 诊断）。纯静态字符串，零外部依赖，
双击即开 —— 浏览器里复盘整份文档的每次 Pass。

    from pdf2zh.v3.inspector_view import build_inspector_html
    html = build_inspector_html(store, decisions, diagnostics, overlays)

JSON 以 ``\\u003c`` 转义后嵌入 <script>，杜绝 `</script>` 注入。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def _js_escape(text: str) -> str:
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def _block_bid_of(node_id: str) -> str:
    """快照 NodeID（``DOC::P1::B0``）→ 既有 diagnostics 的 block_id（``p1_0``）。"""
    node_id = str(node_id)
    if "::" not in node_id:
        return node_id
    parts = node_id.split("::")
    p = b = None
    for part in parts[1:]:
        if part.startswith("P") and p is None:
            p = part[1:]
        elif part.startswith("B"):
            b = part[1:]
    if p is None:
        return node_id
    if b is None:
        return f"p{p}"
    return f"p{p}_{b}"


def _node_lifecycle(snapshots: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """每个 node_id 在各 stage 的 payload + 相邻 stage 字段差异。"""
    by_stage: Dict[str, Dict[str, Any]] = {}
    for stage, snap in (snapshots.get("snapshots") or {}).items():
        by_stage[stage] = (snap or {}).get("nodes") or {}
    from pdf2zh.v3.pass_diff import diff_snapshots

    out: Dict[str, Any] = {}
    stages = list(by_stage)
    for nid in sorted(set().union(*[set(n) for n in by_stage.values()]) or set()):
        present: Dict[str, bool] = {}
        diffs: List[Dict[str, Any]] = []
        for a, b in zip(stages, stages[1:]):
            na, nb = by_stage[a].get(nid, {}), by_stage[b].get(nid, {})
            present[a] = bool(na)
            if na and nb:
                r = diff_snapshots({"nodes": {nid: na}},
                                   {"nodes": {nid: nb}})
                for e in r.entries:
                    if e.kind == "changed":
                        diffs.append({"from": a, "to": b,
                                      "field": e.field,
                                      "before": e.before, "after": e.after})
        present[stages[-1]] = bool(by_stage[stages[-1]].get(nid, {}))
        latest = by_stage[stages[-1]].get(nid, {}) if stages else {}
        out[nid] = {"payload": latest, "present": present, "diffs": diffs[:50]}
    return out


def build_inspector_html(snapshot_store: Any,
                         decisions: Optional[Dict[str, Any]] = None,
                         diagnostics: Optional[Dict[str, Any]] = None,
                         overlays: Optional[List[Dict[str, str]]] = None,
                         title: str = "Document Inspector") -> str:
    """组装自包含 Inspector HTML。

    ``snapshot_store``：SnapshotStore 实例或 ``store.to_dict()``；
    ``overlays``：``[{"page": "Page 1", "svg": "<svg .../>"}]``。
    """
    snap = (snapshot_store.to_dict()
            if hasattr(snapshot_store, "to_dict") else snapshot_store)
    data = {
        "doc_id": snap.get("doc_id", ""),
        "stages": snap.get("stages", []),
        "lifecycle": _node_lifecycle(snap),
        "decisions": decisions or {"counts": {}, "records": []},
        "diagnostics": (diagnostics
                        if isinstance(diagnostics, dict) else
                        {"errors": 0, "warnings": 0, "issues": []}),
        "overlays": overlays or [],
    }
    payload = _js_escape(json.dumps(data, ensure_ascii=False))
    issues = data["diagnostics"].get("issues", [])
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{_js_escape(title)}</title>
<style>
body{{margin:0;font-family:Consolas,monospace;font-size:12px;display:grid;
grid-template-columns:280px 1fr 380px;height:100vh}}
.pane{{overflow:auto;border-right:1px solid #ddd;padding:8px}}
h4{{margin:8px 0 4px;color:#444}}
input{{width:100%;box-sizing:border-box}}
ul{{list-style:none;padding:0;margin:4px 0}}
li{{padding:2px 4px;cursor:pointer;border-radius:3px}}
li:hover{{background:#eef}}
li.sel{{background:#cfe3ff}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:3px}}
.bg{{color:#1b5e20}} .by{{color:#f9a825}} .br{{color:#c62828}}
table{{border-collapse:collapse;width:100%;font-size:11px}}
td,th{{border:1px solid #e0e0e0;padding:2px 4px;text-align:left;vertical-align:top}}
code{{white-space:pre-wrap;word-break:break-all}}
</style></head><body>
<div class="pane" id="treePane"><h4>Nodes</h4><input id="q"
placeholder="filter node id / text"><ul id="tree"></ul></div>
<div class="pane" id="midPane"><h4>Overlay</h4><select id="pg"></select>
<div id="svgBox"></div></div>
<div class="pane" id="rightPane">
<h4>Node</h4><div id="nodeView"></div>
<h4>Lifecycle</h4><div id="lifeView"></div>
<h4>Decisions</h4><div id="decView"></div>
<h4>Diagnostics</h4><div id="diagView"></div>
</div>
<script>
var DATA = {payload};
var lifecycle = DATA.lifecycle, stages = DATA.stages;
var issues = DATA.diagnostics.issues || [];
function esc(s){{s = (s == null) ? '' : String(s);
 return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
function nodes(){{var o=[];for(var k in lifecycle)o.push(k);return o.sort();}}
function renderTree(filter){{var ul=document.getElementById('tree');ul.innerHTML='';
 nodes().forEach(function(id){{if(filter&&id.indexOf(filter)<0)return;
 var li=document.createElement('li');li.textContent=id;
 li.onclick=function(){{select(id);}};ul.appendChild(li);}});}}
function stageDots(id){{
var p=lifecycle[id]||{{}};var h='';
(stages).forEach(function(s,i){{var on=p.present&&p.present[s];
var col=on?'#2e7d32':'#ccc';h+='<span class="dot" title="'+esc(s)+'" style="background:'+col+'"></span>'}});
return h;}}
function stageBadges(id){{
var p=lifecycle[id]||{{}};var h='';var prev='';
for(var i=0;i<stages.length;i++){{
var on=p.present&&p.present[stages[i]];if(on&&prev!==on&&prev!==null)
h+='<span class="bg">+'+esc(stages[i])+'</span>';
if(!on&&prev===true&&prev!==null)h+='<span class="br">-'+esc(stages[i])+'</span>';
prev=on;}}
return h||'<span class="by">stable</span>';}}
function select(id){{
var p=lifecycle[id]||{{}};
var pl=p.payload||{{}};
var rows='';for(var k in pl){{
var v=pl[k];if(typeof v==='object')v=JSON.stringify(v);
rows+='<tr><td>'+esc(k)+'</td><td><code>'+esc(v)+'</code></td></tr>';}}
document.getElementById('nodeView').innerHTML=
'<b>'+esc(id)+'</b> '+stageBadges(id)+'<table>'+
'<tr><th>field</th><th>value</th></tr>'+rows+'</table>';
var dec=DATA.decisions.records.filter(function(d){{return d.node_id===id;}});
document.getElementById('decView').innerHTML=dec.length?
dec.map(function(d){{return '<div><b>'+esc(d.decision)+'</b> conf='+d.confidence+
' src='+esc(d.source)+' stage='+esc(d.stage)+'<br><code>'+esc(JSON.stringify(d.evidence))+'</code></div>';}}).join('')
:'<span class="by">no decisions</span>';
var bid=id.split('::').slice(1).join('_').replace(/P/g,'p').replace(/B/g,'_');
var rel=issues.filter(function(i){{return i.node_id===bid;}});
document.getElementById('diagView').innerHTML=rel.length?
rel.map(function(i){{return '<div class="'+(i.severity==='error'?'br':'by')+
'">'+esc(i.severity)+': '+esc(i.message)+' <code>['+esc(i.node_id)+']</code></div>';}}).join('')
:'<span>none</span>';
var lv='';for(var i=0;i<stages.length;i++){{
lv+='<div>'+stageDots(id)+' <b>'+esc(stages[i])+'</b> '+
(p.present&&p.present[stages[i]]?'<span class="bg">present</span>':'<span class="br">absent</span>')+'</div>';}}
if(p.diffs&&p.diffs.length)lv+='<table><tr><th>Δ</th><th>field</th><th>before</th><th>after</th></tr>'+
p.diffs.map(function(d){{return '<tr><td>'+esc(d.from)+'→'+esc(d.to)+'</td><td>'+esc(d.field)+
'</td><td><code>'+esc(JSON.stringify(d.before))+'</code></td><td><code>'+esc(JSON.stringify(d.after))+'</code></td></tr>';}}).join('')+'</table>';
document.getElementById('lifeView').innerHTML=lv;}}
document.getElementById('q').oninput=function(){{renderTree(this.value);}};
var pg=document.getElementById('pg');var svgBox=document.getElementById('svgBox');
DATA.overlays.forEach(function(o,i){{var op=document.createElement('option');
op.value=i;op.textContent=o.page;pg.appendChild(op);}});
function renderOverlay(){{var o=DATA.overlays[+pg.value];svgBox.innerHTML=o?o.svg:'';}}
pg.onchange=renderOverlay;renderOverlay();renderTree('');
</script></body></html>"""


def build_inspector_html_from_bundle(bundle: Dict[str, Any],
                                     overlays: Optional[List[Dict[str, str]]] = None,
                                     title: str = "Document Inspector") -> str:
    """从 ObsSession.bundle() 直接组装（mainline side-channel 出口）。"""
    return build_inspector_html(
        bundle.get("snapshots") or {},
        decisions=bundle.get("decisions"),
        diagnostics=bundle.get("diagnostics"),
        overlays=overlays, title=title)


__all__ = ["build_inspector_html", "build_inspector_html_from_bundle",
           "_block_bid_of", "_node_lifecycle"]