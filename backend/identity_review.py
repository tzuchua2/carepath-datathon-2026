"""Trace named sites to individual source quotes and group likely duplicate records."""
from collections import defaultdict
from hashlib import sha256
import re

from backend.service_evidence import _SITE, _SITE_SUBJECT, _name_tokens


def _words(value):
    return " ".join(re.findall(r"[^\W_]+", (value or "").casefold()))


def _contains_phrase(text, phrase):
    return bool(phrase and (" " + phrase + " ") in (" " + text + " "))


def duplicate_groups(facilities):
    """Retain each source row. Group only repeated IDs or exact normalized locality/name/PIN."""
    parents = {}
    bases = defaultdict(set)

    def root(k):
        parents.setdefault(k, k)
        while parents[k] != k:
            parents[k] = parents[parents[k]]
            k = parents[k]
        return k

    def link(keys, basis):
        if len(keys) < 2:
            return
        anchor = min(root(k) for k in keys)
        for k in keys:
            parents[root(k)] = anchor
            bases[k].add(basis)

    ids, addresses = defaultdict(list), defaultdict(list)
    for r in facilities:
        k = r['record_key']
        ids[r['unique_id'].lower()].append(k)
        locality = tuple(r.get(c) for c in ('name_key', 'pincode', 'state_key', 'city_key'))
        if all(locality):
            addresses[locality].append(k)
    for keys in ids.values():
        link(keys, 'same_source_facility_id')
    for keys in addresses.values():
        link(keys, 'same_normalized_name_pin_city_state')
    groups = defaultdict(list)
    for r in facilities:
        groups[root(r['record_key'])].append(r)
    result = {}
    for members in groups.values():
        anchor = min(r['unique_id'].lower() for r in members)
        group = 'dg_' + sha256(anchor.encode()).hexdigest()[:32]
        basis = sorted({b for r in members for b in bases[r['record_key']]})
        for r in members:
            result[r['record_key']] = dict(duplicate_group_id=group, duplicate_group_size=len(members),
                                           duplicate_group_basis=basis or ['single_record'])
    return result


def attribute(citation, facility, peers):
    """Label explicit mentions conservatively. Co-occurrence alone cannot identify the operator."""
    quote, kind = citation['quote'], citation['kind']
    normalized, name = _words(quote), _words(facility['name'])
    own = _contains_phrase(normalized, name)
    foreign = set()
    for other in peers:
        key = _words(other)
        if key != name and key not in name and name not in key and _contains_phrase(normalized, key):
            foreign.add(other)
    for pattern in (_SITE, _SITE_SUBJECT):
        for m in pattern.finditer(quote):
            label = m.group(1)
            site = _words(label)
            if site == name or _contains_phrase(name, site):
                continue
            distinctive = _name_tokens(label) - _name_tokens(facility.get('city_key'))
            own_tokens = _name_tokens(facility['name']) - _name_tokens(facility.get('city_key'))
            if distinctive and distinctive.isdisjoint(own_tokens):
                foreign.add(label)
    mentions = sorted(foreign)
    if own and mentions:
        status = 'multiple_named_sites'
    elif mentions:
        status = 'another_site_named'
    elif kind in ('other_site', 'branch_unclear', 'identity_conflict'):
        status = 'site_unresolved'
    elif own and kind == 'direct' and (normalized.startswith(name+' ') or
                                       _contains_phrase(normalized,'at '+name)):
        status = 'own_site_named_in_direct_statement'
    elif own:
        status = 'own_site_mentioned'
    elif len(peers) > 1:
        status = 'unattributed_shared_source'
    else:
        status = 'no_site_named'
    return status, mentions


def identity_and_quotes(facilities, evidence):
    by_key = {r['record_key']:r for r in facilities}
    groups = duplicate_groups(facilities)
    peers = defaultdict(set)
    for r in facilities:
        if r['source_content_id']:
            peers[r['source_content_id']].add(r['name'])
    quotes = []
    per_record = defaultdict(set)
    own_direct = set()
    for row in evidence:
        k = row['record_key']
        f = by_key[k]
        for ordinal, c in enumerate(row['citations']):
            status, names = attribute(c, f, peers.get(f['source_content_id'], set()))
            per_record[k].add(status)
            if status == 'own_site_named_in_direct_statement':
                own_direct.add((k,row['service']))
            quotes.append(dict(record_key=k, unique_id=row['unique_id'], service=row['service'],
                               citation_ordinal=ordinal, **c, site_attribution=status, other_site_mentions=names,
                               record_source_urls=row['record_source_urls']))
    identity = {}
    for k, f in by_key.items():
        flags = set(f['identity_flags'])
        attribution = per_record[k]
        if 'description_names_other_facility_and_city' in flags or attribution & {'another_site_named','multiple_named_sites'}:
            severity = 'high'
        elif 'site_unresolved' in attribution or 'record_id_has_multiple_versions' in flags:
            severity = 'elevated'
        elif 'source_content_shared_across_names' in flags:
            severity = 'shared_source_only'
        elif 'possible_duplicate_name_and_pin' in flags:
            severity = 'possible_duplicate'
        else:
            severity = 'no_signal_detected'
        identity[k] = dict(**groups[k],identity_severity=severity,
                           identity_review_reasons=sorted(flags | (attribution & {
                               'another_site_named','multiple_named_sites','site_unresolved'})))
    return identity, quotes, own_direct
