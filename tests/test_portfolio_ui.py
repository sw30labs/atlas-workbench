"""Execute the actual scenario renderer against a minimal offline DOM harness."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from contingency_atlas.web import server


def test_scenario_paging_limits_cards_and_preserves_full_filtered_selection():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the scenario renderer")
    html = (Path(server.__file__).parent / "static/index.html").read_text()
    source = "const scnPaging =" + html.split("const scnPaging =", 1)[1].split("const evidenceEventState =", 1)[0]
    listener = next(line for line in html.splitlines() if line.startswith('$("#scn-search").addEventListener("input"'))
    harness = r'''
const nodes={};
class Element {
 constructor(id=''){this.id=id;this.children=[];this.events={};this.dataset={};this.disabled=false;}
 set innerHTML(value){
  this.html=value;for(const c of this.children)if(c.id)delete nodes['#'+c.id];this.children=[];
  for(const m of value.matchAll(/<button[^>]*id="([^"]+)"([^>]*)>/g)){
   const child=new Element(m[1]);child.disabled=m[2].includes('disabled');nodes['#'+child.id]=child;this.children.push(child);
  }
  for(const m of value.matchAll(/class="scn-card" data-id="([^"]+)"/g)){
   const child=new Element();child.card=true;child.dataset.id=m[1];this.children.push(child);
  }
 }
 get innerHTML(){return this.html||'';}
 after(element){nodes['#'+element.id]=element;}
 addEventListener(event,callback){this.events[event]=callback;}
 click(){if(!this.disabled)this.events.click?.();}
 querySelector(){return this.children.find(x=>!x.disabled)||null;}
 scrollIntoView(){} focus(){}
}
for(const id of ['scn-grid','scn-count','scn-search'])nodes['#'+id]=new Element(id);
const $=selector=>nodes[selector]||null;
const $$=selector=>selector==='#scn-grid .scn-card'?$('#scn-grid').children.filter(x=>x.card):[];
const document={createElement:()=>new Element()};
const esc=String,catColor=()=> '#123456',urgColor=()=> '#123456';
const requestAnimationFrame=callback=>callback();
let opened=null;const openScenario=id=>opened=id;
let DATA={scenarios:Array.from({length:50},(_,index)=>({id:'SYN-'+String(index).padStart(3,'0'),
 title:'Scenario '+String(index).padStart(3,'0'),description:'Synthetic fixture',
 category:'Synthetic category',origin:'synthetic_fixture',status:'active',urgency_score:50-index}))};
SOURCE
let deb, scheduled;const setTimeout=callback=>{scheduled=callback;return 1;},clearTimeout=()=>{};
LISTENER
const capture=()=>({page:scnPaging.page,ids:$$('#scn-grid .scn-card').map(x=>x.dataset.id),
 fullFiltered:scnFiltered().length});
renderScenarios();const first=capture();
$('#scn-next-page').click();const next=capture();$$('#scn-grid .scn-card')[0].click();
$('#scn-next-page').click();const last=capture();
scnState.q='Scenario 000';renderScenarios();const filtered=capture();
scnState.q='';renderScenarios();scnPaging.page=2;renderScenarios();
DATA.scenarios=DATA.scenarios.slice(0,10);renderScenarios();const reduced=capture();
$('#scn-search').events.input({target:{value:'  Scenario 001  '}});
const immediateSelection=scnFiltered().map(x=>x.id);
console.log(JSON.stringify({first,next,last,filtered,reduced,opened,immediateSelection}));
'''.replace("SOURCE", source).replace("LISTENER", listener)
    completed = subprocess.run([node, "-"], input=harness, text=True, capture_output=True, check=True, timeout=10)
    result = json.loads(completed.stdout)
    assert len(result["first"]["ids"]) == len(result["next"]["ids"]) == 24
    assert result["first"]["ids"][0] == "SYN-000" and result["next"]["ids"][0] == "SYN-024"
    assert result["next"]["page"] == 1 and result["next"]["fullFiltered"] == 50
    assert result["opened"] == "SYN-024"
    assert result["last"]["ids"] == ["SYN-048", "SYN-049"]
    assert result["filtered"]["page"] == 0 and result["filtered"]["ids"] == ["SYN-000"]
    assert result["reduced"]["page"] == 0 and len(result["reduced"]["ids"]) == 10
    assert result["immediateSelection"] == ["SYN-001"]
