"""Execute the actual organization form renderer using fictional browser data."""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from contingency_atlas.web import server


class RenderedForm(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags, self.options, self.inputs, self.selects = [], [], [], []
        self.current_option = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        attributes = dict(attrs)
        if tag == "option":
            self.current_option = {"attrs": attributes, "text": ""}
            self.options.append(self.current_option)
        elif tag == "input":
            self.inputs.append(attributes)
        elif tag == "select":
            self.selects.append(attributes)

    def handle_endtag(self, tag):
        if tag == "option":
            self.current_option = None

    def handle_data(self, data):
        if self.current_option is not None:
            self.current_option["text"] += data


@pytest.fixture(scope="module")
def fields():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute organization form controls")
    source = (Path(server.__file__).parent / "static/experience.js").read_text()
    # Exercise production escaping, naming and schema selection along with
    # formField; do not reimplement the renderer's option-selection logic.
    declarations = "\n".join(next(line for line in source.splitlines() if line.strip().startswith(prefix))
                             for prefix in ("const h =", "const label =", "const names=", "function records("))
    renderer = "const refKinds=" + source.split("const refKinds=", 1)[1].split("function recordForm(", 1)[0]
    script = r'''
const DATA={scenarios:[],evidence_events:[
 {event_id:'SYN-CANONICAL',ref:'unused-shadow-alias',headline:'Fictional canonical event'},
 {ref:'SYN-LEGACY',headline:'<img src=x onerror="alert(1)"> & "Fictional"'},
 {id:'SYN-FALLBACK',title:'Fictional fallback event'},
 {ref:'SYN-"<&',headline:'Fictional quoted identifier'}
]};
const memory={context:{records:{services:[
 {id:'svc-one',name:'Fictional first service'},
 {id:'svc-two',name:'Fictional second service'}
]}}};
DECLARATIONS
RENDERER
const relation={type:'string',enum:['corroborates','contradicts','qualifies']};
const result={
 newEvidence:formField('evidence_id',{type:'string'},undefined,true),
 existingEvidence:formField('evidence_id',{type:'string'},'SYN-LEGACY',true),
 escapedEvidence:formField('evidence_id',{type:'string'},'SYN-"<&',true),
 newServices:formField('service_ids',{type:'array'},undefined,false),
 existingServices:formField('service_ids',{type:'array'},['svc-two'],false),
 requiredRelation:formField('relation',relation,undefined,true),
 existingRelation:formField('relation',relation,'contradicts',true),
 defaultRelation:formField('relation',{...relation,default:'qualifies'},undefined,true),
 nullableRequired:formField('relation',{anyOf:[relation,{type:'null'}]},null,true),
 booleanFalse:formField('common_cause',{type:'boolean',default:true},false,false),
 booleanTrue:formField('common_cause',{type:'boolean'},true,false),
 booleanDefault:formField('common_cause',{type:'boolean',default:true},undefined,false),
 booleanAbsent:formField('common_cause',{type:'boolean'},undefined,false),
 escapedText:formField('owner',{type:'string'},'" autofocus onfocus="alert(1)" <script>fictional</script>',true),
 escapedNotes:formField('notes',{type:'string'},'</textarea><script>fictional</script>',false)
};
console.log(JSON.stringify(result));
'''.replace("DECLARATIONS", declarations).replace("RENDERER", renderer)
    completed = subprocess.run([node, "-"], input=script, text=True, capture_output=True, check=True, timeout=10)
    return {key: RenderedForm(value) for key, value in json.loads(completed.stdout).items()}


def selected(form):
    return [option["attrs"]["value"] for option in form.options if "selected" in option["attrs"]]


def test_evidence_choices_resolve_current_legacy_and_fallback_identifiers(fields):
    form = fields["newEvidence"]
    assert [option["attrs"]["value"] for option in form.options] == [
        "", "SYN-CANONICAL", "SYN-LEGACY", "SYN-FALLBACK", 'SYN-"<&',
    ]
    assert form.options[0]["text"] == "Choose…"
    assert selected(form) == []
    assert "required" in form.selects[0] and "multiple" not in form.selects[0]


def test_existing_evidence_and_multiple_service_choices_are_preserved(fields):
    assert selected(fields["existingEvidence"]) == ["SYN-LEGACY"]
    assert selected(fields["escapedEvidence"]) == ['SYN-"<&']
    assert selected(fields["newServices"]) == []
    assert selected(fields["existingServices"]) == ["svc-two"]
    assert "multiple" in fields["existingServices"].selects[0]


@pytest.mark.parametrize("name", ["requiredRelation", "nullableRequired"])
def test_required_enum_requires_explicit_choice_without_a_declared_default(fields, name):
    form = fields[name]
    assert form.options[0] == {"attrs": {"value": ""}, "text": "Choose…"}
    assert selected(form) == [] and "required" in form.selects[0]


def test_existing_enum_and_explicit_schema_default_are_retained(fields):
    assert selected(fields["existingRelation"]) == ["contradicts"]
    assert selected(fields["defaultRelation"]) == ["qualifies"]
    assert all(option["attrs"]["value"] for option in fields["existingRelation"].options)


@pytest.mark.parametrize("name,checked", [
    ("booleanFalse", False), ("booleanTrue", True),
    ("booleanDefault", True), ("booleanAbsent", False),
])
def test_boolean_fields_use_checkboxes_and_preserve_false_over_default(fields, name, checked):
    controls = fields[name].inputs
    assert len(controls) == 1 and controls[0]["type"] == "checkbox"
    assert ("checked" in controls[0]) is checked


def test_option_labels_identifiers_text_and_textareas_are_escaped(fields):
    evidence = fields["newEvidence"]
    assert "img" not in evidence.tags and "script" not in evidence.tags
    assert evidence.options[2]["text"] == '<img src=x onerror="alert(1)"> & "Fictional"'
    assert all(set(option["attrs"]) <= {"value", "selected"} for option in evidence.options)
    text = fields["escapedText"]
    assert "script" not in text.tags
    assert "autofocus" not in text.inputs[0] and "onfocus" not in text.inputs[0]
    assert text.inputs[0]["value"] == '" autofocus onfocus="alert(1)" <script>fictional</script>'
    assert fields["escapedNotes"].tags.count("textarea") == 1
    assert "script" not in fields["escapedNotes"].tags
