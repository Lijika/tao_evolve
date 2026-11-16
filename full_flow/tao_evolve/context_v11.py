"""Deterministic compact packets. Byte budget is NOT a token or money guarantee."""
import json

def compact_packet(mandatory, optional_sections, max_utf8_bytes):
    """Keep all mandatory rule, identity, failure, matrix and unknown fields intact.
    optional_sections: (evidence_id, text) in caller's priority order.
    Drop whole optional sections, retain pointers; never slice JSON halfway.
    """
    if max_utf8_bytes<=0:raise ValueError('Invalid byte limit')
    def encode(d):return json.dumps(d,ensure_ascii=False,separators=(',',':'))
    optional_sections = tuple(optional_sections)
    refs=[str(k) for k,_ in optional_sections]
    if len(set(refs)) != len(refs):raise ValueError('Duplicate evidence IDs')
    if any(not isinstance(k,str) or not k for k,_ in optional_sections):raise ValueError('Bad evidence ID')
    packet={'mandatory':mandatory,'selected':{},'omitted_evidence_ids':refs}
    if len(encode(packet).encode('utf-8'))>max_utf8_bytes:
        raise ValueError('Mandatory safety packet exceeds limit; fetch less optional context, do not truncate rules')
    for key,text in optional_sections:
        trial={'mandatory':mandatory,'selected':dict(packet['selected']),
               'omitted_evidence_ids':list(packet['omitted_evidence_ids'])}
        trial['selected'][key]=text
        trial['omitted_evidence_ids'].remove(str(key))
        if len(encode(trial).encode('utf-8'))<=max_utf8_bytes:packet=trial
    return encode(packet)
