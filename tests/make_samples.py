"""Build realistic TIA V20 SimaticML exports (old + new) to test PLC Diff."""
import sys
from pathlib import Path

FLG = "http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v5"
IFC = "http://www.siemens.com/automation/Openness/SW/Interface/v5"


class NB:
    """Tiny network builder producing FlgNet XML."""
    def __init__(self):
        self.uid = 20
        self.parts, self.wires = [], []

    def _u(self):
        self.uid += 1
        return str(self.uid)

    def glob(self, *comps):
        u = self._u()
        c = "".join('<Component Name="%s" />' % x for x in comps)
        self.parts.append('<Access Scope="GlobalVariable" UId="%s"><Symbol>%s</Symbol></Access>' % (u, c))
        return u

    def loc(self, *comps):
        u = self._u()
        c = "".join('<Component Name="%s" />' % x for x in comps)
        self.parts.append('<Access Scope="LocalVariable" UId="%s"><Symbol>%s</Symbol></Access>' % (u, c))
        return u

    def const(self, val, typ=None):
        u = self._u()
        if typ:
            self.parts.append('<Access Scope="TypedConstant" UId="%s"><Constant><ConstantValue>%s</ConstantValue></Constant></Access>' % (u, val))
        else:
            self.parts.append('<Access Scope="LiteralConstant" UId="%s"><Constant><ConstantType>Int</ConstantType><ConstantValue>%s</ConstantValue></Constant></Access>' % (u, val))
        return u

    def part(self, name, neg=None, inst=None, tmpl=None):
        u = self._u()
        inner = ""
        if neg:
            inner += '<Negated Name="%s" />' % neg
        if inst:
            inner += '<Instance Scope="LocalVariable" UId="%s"><Component Name="%s" /></Instance>' % (self._u(), inst)
        for k, v in (tmpl or {}).items():
            inner += '<TemplateValue Name="%s" Type="Type">%s</TemplateValue>' % (k, v)
        self.parts.append('<Part Name="%s" UId="%s"%s' % (name, u, ">%s</Part>" % inner if inner else " />"))
        return u

    def call(self, name, btype, inst, params):
        u = self._u()
        ps = "".join('<Parameter Name="%s" Section="%s" Type="%s" />' % p for p in params)
        ins = ('<Instance Scope="GlobalVariable" UId="%s"><Component Name="%s" /></Instance>' % (self._u(), inst)) if inst else ""
        self.parts.append('<Call UId="%s"><CallInfo Name="%s" BlockType="%s">%s%s</CallInfo></Call>' % (u, name, btype, ins, ps))
        return u

    def w(self, src, *dests):
        def ep(e):
            if e == "rail":
                return "<Powerrail />"
            if isinstance(e, tuple):
                return '<NameCon UId="%s" Name="%s" />' % e
            return '<IdentCon UId="%s" />' % e
        self.wires.append('<Wire UId="%s">%s</Wire>' % (self._u(), "".join(ep(x) for x in (src,) + dests)))

    def xml(self):
        return ('<FlgNet xmlns="%s"><Parts>%s</Parts><Wires>%s</Wires></FlgNet>' % (FLG, "".join(self.parts), "".join(self.wires)))


def contact(nb, op_uid, neg=False):
    c = nb.part("Contact", neg="operand" if neg else None)
    nb.w(op_uid, (c, "operand"))
    return c


def coil(nb, op_uid, name="Coil"):
    c = nb.part(name)
    nb.w(op_uid, (c, "operand"))
    return c


def ml(comp, text, uid):
    return ('<MultilingualText ID="%s" CompositionName="%s"><ObjectList><MultilingualTextItem ID="%s" CompositionName="Items">'
            '<AttributeList><Culture>en-US</Culture><Text>%s</Text></AttributeList></MultilingualTextItem></ObjectList></MultilingualText>'
            % (uid, comp, uid + "1", text))


def cu(i, title, flg, lang="LAD"):
    return ('<SW.Blocks.CompileUnit ID="%X" CompositionName="CompileUnits"><AttributeList><NetworkSource>%s</NetworkSource>'
            '<ProgrammingLanguage>%s</ProgrammingLanguage></AttributeList><ObjectList>%s%s</ObjectList></SW.Blocks.CompileUnit>'
            % (i * 16, flg, lang, ml("Comment", "", "%XA" % i), ml("Title", title, "%XB" % i)))


def fc(name, number, iface, units):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<Document><Engineering version="V20" /><SW.Blocks.FC ID="0"><AttributeList>'
            '<Interface><Sections xmlns="%s">%s</Sections></Interface><MemoryLayout>Optimized</MemoryLayout><Name>%s</Name>'
            '<Number>%s</Number><ProgrammingLanguage>LAD</ProgrammingLanguage></AttributeList><ObjectList>%s%s'
            '</ObjectList></SW.Blocks.FC></Document>' % (IFC, iface, name, number, "".join(units), ml("Title", "Station 1 calls", "FF")))


def iface(temps):
    secs = ['<Section Name="Input" />', '<Section Name="Output" />', '<Section Name="InOut" />']
    secs.append('<Section Name="Temp">%s</Section>' % "".join('<Member Name="%s" Datatype="%s" />' % t for t in temps))
    secs.append('<Section Name="Constant" /><Section Name="Return"><Member Name="Ret_Val" Datatype="Void" /></Section>')
    return "".join(secs)


# ---------------------------------------------------------------------------

def net_motor(new):
    nb = NB()
    start, stop, seal, out = nb.glob("I_Start"), nb.glob("I_Stop"), nb.glob("Q_Motor"), nb.glob("Q_Motor")
    c1, c2 = contact(nb, start), contact(nb, seal)
    o = nb.part("O", tmpl={"Card": "2"})
    c3 = contact(nb, stop, neg=True)
    q = coil(nb, out)
    nb.w("rail", (c1, "in"), (c2, "in"))
    nb.w((c1, "out"), (o, "in1"))
    nb.w((c2, "out"), (o, "in2"))
    nb.w((o, "out"), (c3, "in"))
    if new:
        door = nb.glob("I_DoorClosed")
        c4 = contact(nb, door)
        nb.w((c3, "out"), (c4, "in"))
        nb.w((c4, "out"), (q, "in"))
    else:
        nb.w((c3, "out"), (q, "in"))
    return cu(1, "Motor start / seal-in", nb.xml())


def net_jam(new):
    nb = NB()
    run, sens, alarm, pt, et = nb.glob("Conv", "Running"), nb.glob("Conv", "Sensor"), nb.glob("Alarm", "Jam"), \
        nb.const("T#5s" if new else "T#3s", typ=True), nb.loc("etJam")
    c1, c2 = contact(nb, run), contact(nb, sens, neg=True)
    t = nb.part("TON", inst="T_Jam", tmpl={"time_type": "Time"})
    q = coil(nb, alarm)
    nb.w("rail", (c1, "in"))
    nb.w((c1, "out"), (c2, "in"))
    nb.w((c2, "out"), (t, "IN"))
    nb.w(pt, (t, "PT"))
    nb.w((t, "Q"), (q, "in"))
    nb.w((t, "ET"), et)
    return cu(2, "Conveyor jam timer", nb.xml())


def net_call(new):
    nb = NB()
    s = nb.glob("M_AutoStart" if new else "M_Start")
    st, busy, err = nb.glob("M_Stop"), nb.glob("M_Busy"), nb.glob("Alarm", "Station")
    c = nb.call("FB_Station", "FB", "FB_Station_DB",
                [("Start", "Input", "Bool"), ("Stop", "Input", "Bool"), ("Busy", "Output", "Bool"), ("Error", "Output", "Bool")])
    nb.w("rail", (c, "en"))
    nb.w(s, (c, "Start"))
    nb.w(st, (c, "Stop"))
    nb.w((c, "Busy"), busy)
    nb.w((c, "Error"), err)
    return cu(3, "Station sequence", nb.xml())


def net_reset():
    nb = NB()
    r, a1, a2, hmi = nb.glob("I_Reset"), nb.glob("Alarm", "Jam"), nb.glob("Alarm", "Door"), nb.glob("HMI", "Reset")
    c1, c2 = contact(nb, r), contact(nb, hmi)
    o = nb.part("O", tmpl={"Card": "2"})
    k1, k2 = coil(nb, a1, "RCoil"), coil(nb, a2, "RCoil")
    nb.w("rail", (c1, "in"), (c2, "in"))
    nb.w((c1, "out"), (o, "in1"))
    nb.w((c2, "out"), (o, "in2"))
    nb.w((o, "out"), (k1, "in"), (k2, "in"))
    return cu(4, "Reset alarms", nb.xml())


def net_lamp():
    nb = NB()
    cnt, ten, lamp = nb.glob("Count"), nb.const("10"), nb.glob("Q_Lamp")
    auto = nb.glob("M_Auto")
    c = nb.part("Gt", tmpl={"SrcType": "Int"})
    a = contact(nb, auto)
    q = coil(nb, lamp, "SCoil")
    nb.w("rail", (a, "in"))
    nb.w((a, "out"), (c, "pre"))
    nb.w(cnt, (c, "in1"))
    nb.w(ten, (c, "in2"))
    nb.w((c, "out"), (q, "in"))
    return cu(5, "Counter lamp", nb.xml())


def net_old_test():
    nb = NB()
    t, q = nb.glob("M_Test"), nb.glob("Q_TestLamp")
    c = contact(nb, t)
    k = coil(nb, q)
    nb.w("rail", (c, "in"))
    nb.w((c, "out"), (k, "in"))
    return cu(5, "Test lamp (temporary)", nb.xml())


def block(new):
    units = [net_motor(new), net_jam(new), net_call(new), net_reset()]
    units.append(net_lamp() if new else net_old_test())
    temps = [("etJam", "Time")] + ([("tmpFlag", "Bool")] if new else [])
    return fc("FC_Station", "12", iface(temps), units)


def fbd_ext(new):
    nb = NB()
    a1, a2, ack, out = nb.glob("I_ExtFault1"), nb.glob("I_ExtFault2"), nb.glob("HMI", "Ack"), nb.glob("Alarm", "Ext")
    c = nb.call("FC_ExternalAlarm", "FC", None, [("triggerMessage_1", "Input", "Bool"), ("triggerMessage_2", "Input", "Bool"),
                                                     ("ack", "Input", "Bool"), ("alarm", "Output", "Bool")])
    if not new:
        nb.parts[-1] = nb.parts[-1].replace('<Call UId="%s">' % c, '<Call UId="%s"><Negated Name="triggerMessage_1" />' % c)
    nb.w(a1, (c, "triggerMessage_1"))
    nb.w(a2, (c, "triggerMessage_2"))
    nb.w(ack, (c, "ack"))
    nb.w((c, "alarm"), out)
    return cu(1, "External faults", nb.xml(), "FBD")


def fbd_motor(new):
    nb = NB()
    auto, fault, start, run, q = nb.glob("M_Auto"), nb.glob("M_Fault"), nb.glob("I_Start"), nb.glob("Q_Running"), nb.glob("Q_Enable")
    o = nb.part("O", tmpl={"Card": "2"})
    a = nb.part("A", neg="in2", tmpl={"Card": "3"})
    k = coil(nb, q)
    nb.w(start, (o, "in1"))
    nb.w(run, (o, "in2"))
    nb.w(auto, (a, "in1"))
    nb.w(fault, (a, "in2"))
    nb.w((o, "out"), (a, "in3"))
    nb.w((a, "out"), (k, "in"))
    if new:
        r = nb.glob("M_ResetLatch")
        a2 = nb.part("A", tmpl={"Card": "2"})
        s2 = coil(nb, r, "RCoil")
        nb.w(fault, (a2, "in1"))
        nb.w(ack := nb.glob("HMI", "Ack"), (a2, "in2"))
        nb.w((a2, "out"), (s2, "in"))
    return cu(2, "Motor enable", nb.xml(), "FBD")


def fbd_direct(new):
    """FBD "Q_Ready" = "I_Ready": a tag wired straight into a coil, no gate in between."""
    nb = NB()
    src, q = nb.glob("I_ReadyOk" if new else "I_Ready"), nb.glob("Q_Ready")
    k = coil(nb, q)
    nb.w(src, (k, "in"))
    return cu(3, "Ready lamp", nb.xml(), "FBD")


def fbd_fc(new):
    units = [fbd_ext(new), fbd_motor(new), fbd_direct(new)]
    return fc("FC_Alarms", "176", iface([]), units).replace("<ProgrammingLanguage>LAD</ProgrammingLanguage></AttributeList><ObjectList>",
                                                                 "<ProgrammingLanguage>FBD</ProgrammingLanguage></AttributeList><ObjectList>")


GRA = "http://www.siemens.com/automation/Openness/SW/NetworkSource/Graph/v6"


def graph_fb(new):
    """A small S7-GRAPH sequence: S1 Home -> T1 -> S2 Clamp -> T2 -> S3 Weld -> T3 -> jump to S1.
    New version: S2 renamed with a longer max time, T2 checks another tag, S3's action changed."""
    def cond(tag):
        nb = NB()
        a, c = nb.glob(tag), nb.part("TrCoil")
        nb.w(a, (c, "in"))
        return nb.xml()

    def step(num, name, tmax, action, init=False):
        return ('<Step Number="%d" Init="%s" Name="%s" MaximumStepTime="%s" WarningTime="T#5S"><Actions>'
                '<Action Qualifier="N"><Token Text="&quot;%s&quot;" /></Action></Actions></Step>' % (
                    num, "true" if init else "false", name, tmax, action))

    steps = [step(1, "Home", "T#10S", "Q_Home", True),
             step(2, "Clamp part" if new else "Clamp", "T#15S" if new else "T#10S", "Q_Clamp"),
             step(3, "Weld", "T#10S", "Q_WeldOn" if new else "Q_Weld")]
    trans = [(1, "Start", "I_Start"), (2, "Clamped", "I_ClampedOk" if new else "I_Clamped"), (3, "Welded", "I_WeldDone")]
    tr = "".join('<Transition IsMissing="false" Name="%s" Number="%d" ProgrammingLanguage="FBD">%s</Transition>' % (
        n, i, cond(t)) for i, n, t in trans)
    links = [("StepRef", 1, "TransitionRef", 1, "Direct"), ("TransitionRef", 1, "StepRef", 2, "Direct"),
             ("StepRef", 2, "TransitionRef", 2, "Direct"), ("TransitionRef", 2, "StepRef", 3, "Direct"),
             ("StepRef", 3, "TransitionRef", 3, "Direct"), ("TransitionRef", 3, "StepRef", 1, "Jump")]
    con = "".join('<Connection><NodeFrom><%s Number="%d" /></NodeFrom><NodeTo><%s Number="%d" /></NodeTo>'
                  '<LinkType>%s</LinkType></Connection>' % x for x in links)
    graph = ('<Graph xmlns="%s"><Sequence><Steps>%s</Steps><Transitions>%s</Transitions><Branches />'
             '<Connections>%s</Connections></Sequence></Graph>' % (GRA, "".join(steps), tr, con))
    unit = ('<SW.Blocks.CompileUnit ID="10" CompositionName="CompileUnits"><AttributeList><NetworkSource>%s</NetworkSource>'
            '<ProgrammingLanguage>GRAPH</ProgrammingLanguage></AttributeList><ObjectList>%s</ObjectList>'
            '</SW.Blocks.CompileUnit>' % (graph, ml("Title", "Sequence", "1A")))
    return ('<?xml version="1.0" encoding="utf-8"?>\n<Document><Engineering version="V20" /><SW.Blocks.FB ID="0"><AttributeList>'
            '<Interface><Sections xmlns="%s"><Section Name="Input" /><Section Name="Output" /><Section Name="InOut" />'
            '<Section Name="Static" /><Section Name="Temp" /><Section Name="Constant" /></Sections></Interface>'
            '<Name>FB_WeldSequence</Name><Number>40</Number><ProgrammingLanguage>GRAPH</ProgrammingLanguage></AttributeList>'
            '<ObjectList>%s%s</ObjectList></SW.Blocks.FB></Document>' % (IFC, unit, ml("Title", "Weld sequence", "F2")))


def db(new):
    """A global DB: a start value, a data type and the order of members change, one member is added."""
    rows = [("Speed", "Int", "1200"), ("Ramp", "Time", "T#2S"), ("Mode", "Byte", ""), ("Count", "DInt", "")]
    if new:
        rows = [("Speed", "Int", "1500"), ("Mode", "Byte", ""), ("Ramp", "Time", "T#2S"), ("Count", "LInt", ""),
                ("Limit", "Real", "99.5")]
    m = "".join('<Member Name="%s" Datatype="%s">%s</Member>' % (n, d, "<StartValue>%s</StartValue>" % v if v else "")
                for n, d, v in rows)
    return ('<?xml version="1.0" encoding="utf-8"?>\n<Document><Engineering version="V20" /><SW.Blocks.GlobalDB ID="0">'
            '<AttributeList><Interface><Sections xmlns="%s"><Section Name="Static">%s</Section></Sections></Interface>'
            '<MemoryLayout>Standard</MemoryLayout><Name>DB_Settings</Name><Number>20</Number>'
            '<ProgrammingLanguage>DB</ProgrammingLanguage></AttributeList><ObjectList>%s</ObjectList>'
            '</SW.Blocks.GlobalDB></Document>' % (IFC, m, ml("Title", "Station settings", "F1")))


def tags(new):
    rows = [("I_Start", "Bool", "%I0.0"), ("I_Stop", "Bool", "%I0.1"), ("I_Reset", "Bool", "%I0.2"),
            ("Q_Motor", "Bool", "%Q0.0"), ("Q_TestLamp", "Bool", "%Q0.1")]
    if new:
        rows = rows[:3] + [("I_DoorClosed", "Bool", "%I1.3")] + rows[3:4] + [("Q_Lamp", "Bool", "%Q0.2")]
    t = "".join('<SW.Tags.PlcTag ID="%d" CompositionName="Tags"><AttributeList><DataTypeName>%s</DataTypeName>'
                '<LogicalAddress>%s</LogicalAddress><Name>%s</Name></AttributeList></SW.Tags.PlcTag>' % (i, d, a, n)
                for i, (n, d, a) in enumerate(rows, 1))
    return ('<?xml version="1.0" encoding="utf-8"?>\n<Document><Engineering version="V20" /><SW.Tags.PlcTagTable ID="0">'
            '<AttributeList><Name>Station1_IO</Name></AttributeList><ObjectList>%s</ObjectList></SW.Tags.PlcTagTable></Document>' % t)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    which = sys.argv[2]
    new = which == "new"
    (out / "vci" / "Program blocks" / "Station").mkdir(parents=True, exist_ok=True)
    (out / "vci" / "PLC tags").mkdir(parents=True, exist_ok=True)
    (out / "vci" / "Program blocks" / "Station" / "FC_Station.xml").write_text(block(new), encoding="utf-8")
    (out / "vci" / "Program blocks" / "Station" / "FC_Alarms.xml").write_text(fbd_fc(new), encoding="utf-8")
    (out / "vci" / "PLC tags" / "Station1_IO.xml").write_text(tags(new), encoding="utf-8")
    (out / "vci" / "Program blocks" / "Station" / "DB_Settings.xml").write_text(db(new), encoding="utf-8")
    (out / "vci" / "Program blocks" / "Station" / "FB_WeldSequence.xml").write_text(graph_fb(new), encoding="utf-8")
