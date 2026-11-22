"""Assemble CareProof v3 from the pinned v2 release and validated city/office output."""
import argparse
import csv
from collections import Counter
from hashlib import sha256
from pathlib import Path
import json
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from backend.identity_review import identity_and_quotes

DATA = Path(__file__).resolve().parents[1] / 'data'
V2 = DATA / 'derived'
ORIGINALS = ('postal_reference','district_indicators','facility_quarantine','external_corroboration')
SITE = ('identity_severity','identity_review_reasons','duplicate_group_id','duplicate_group_size','duplicate_group_basis')
GEO = ('best_estimate_latitude','best_estimate_longitude','best_estimate_basis','best_estimate_confidence',
       'best_estimate_precision','city_peer_distance_km','city_peer_count','city_peer_status','location_flags')


CONTACT = ('address_display','address_search_native','address_search_romanized','address_scripts',
           'unavailable_field_count','majority_unavailable','phone_status','phone_flags',
           'official_phone_e164','website_status','website_flags','official_web_host')


def verify_v2():
    for line in (DATA/'recovery/v2.sha256').read_text().splitlines():
        digest, path = line.split(None, 1)
        if sha256((DATA.parent/path.strip()).read_bytes()).hexdigest() != digest:
            raise ValueError('Pinned v2 input changed: '+path)


def write_geo(staging):
    from pyspark.sql import SparkSession
    from backend.geography_review import review_facility_geography, build_place_lookup

    spark = SparkSession.builder.master('local[2]').config('spark.sql.shuffle.partitions','4').getOrCreate()
    try:
        fac=review_facility_geography(spark.read.parquet(str(V2/'clean_facilities.parquet')))
        fac.write.parquet(str(staging/'facilities'))
        places=build_place_lookup(spark.read.parquet(str(DATA/'india_post_pincode_directory.parquet')),
                                  spark.read.parquet(str(V2/'postal_reference.parquet')),
                                  spark.read.parquet(str(staging/'facilities')))
        places.write.parquet(str(staging/'places'))
    finally:
        spark.stop()


def add_fields(table, rows, columns):
    for key in columns:
        table=table.append_column(key,pa.array([r[key] for r in rows]))
    return table


def replace(table, name, values):
    idx=table.schema.get_field_index(name)
    return table.set_column(idx,table.field(name),pa.array(values,type=table.field(name).type))


def assemble(geo, output):
    verify_v2()
    clean=pq.read_table(geo/'facilities').sort_by([('record_key','ascending')])
    records=clean.to_pylist()
    by_key={r['record_key']:r for r in records}
    if len(by_key)!=len(records):
        raise ValueError('Duplicate facility key')
    baseline=pq.read_table(V2/'clean_facilities.parquet').sort_by([('record_key','ascending')])
    raw_fields=pq.read_schema(DATA/'facilities.parquet').names
    preserved=['record_key','source_row_count',*raw_fields]
    if not clean.select(preserved).equals(baseline.select(preserved)):
        raise ValueError('Source record changed')
    with (DATA.parent/'references/verified_service_sources.csv').open(newline='',encoding='utf-8') as handle:
        conflicts={r['record_key'] for r in csv.DictReader(handle) if r['external_location_conflict']=='true'}
    for k in conflicts & by_key.keys():
        f=by_key[k]
        f['location_flags'] = list(dict.fromkeys([*f['location_flags'],'external_source_address_conflict']))
        f['location_review']=True
        for field in ('usable_latitude','usable_longitude','best_estimate_latitude','best_estimate_longitude'):
            f[field]=None
        f.update(best_estimate_basis='unresolved',best_estimate_confidence='unknown',best_estimate_precision='unknown')
    for field in ('location_flags','location_review','usable_latitude','usable_longitude',*GEO[:5]):
        clean=replace(clean,field,[r[field] for r in records])
    contacts=pq.read_table(DATA/'enrichment/address_contact_quality.parquet',columns=['record_key','v2_disposition',*CONTACT])
    clean_contacts={r['record_key']:r for r in contacts.to_pylist() if r['v2_disposition']=='clean'}
    if clean_contacts.keys()!=by_key.keys():
        raise ValueError('Address/contact keys differ from the cleaned release')
    addr_rows=[clean_contacts[r['record_key']] for r in records]
    for field in CONTACT:
        clean=clean.append_column(field,pa.array([r[field] for r in addr_rows],type=contacts.field(field).type))
    pairs=pq.read_table(V2/'service_evidence.parquet').sort_by([('record_key','ascending'),('service','ascending')])
    evidence=pairs.to_pylist()
    identity,quotes,own_direct=identity_and_quotes(records,evidence)
    id_rows=[identity[r['record_key']] for r in records]
    clean=add_fields(clean,id_rows,SITE)
    for f,item in zip(records,id_rows):
        f.update(item)
        if item['identity_severity'] in ('high','elevated') and not f['identity_review']:
            f['identity_review']=True
            f['identity_flags']=[*f['identity_flags'],'citation_site_review']
    for field in ('identity_review','identity_flags'):
        clean=replace(clean,field,[r[field] for r in records])
    for e in evidence:
        k=e['record_key']
        f=by_key[k]
        for field in ('location_review','identity_review'):
            if f[field]:
                if field not in e['flags']:
                    e['flags'].append(field)
                e['eligible']=False
        direct=any(q['kind']=='direct' for q in e['citations'])
        e['has_own_site_direct_quote']=(k,e['service']) in own_direct
        e['review_priority_basis']=('in_candidate_table' if e['eligible'] else
            'own_site_direct_statement' if e['has_own_site_direct_quote'] else
            'direct_statement_needs_attribution' if direct else 'other_evidence')
        e.update({field:f[field] for field in (*SITE,*GEO)})
    pairs=replace(replace(pairs,'flags',[r['flags'] for r in evidence]),'eligible',[r['eligible'] for r in evidence])
    new_cols=(*SITE,*GEO,'has_own_site_direct_quote','review_priority_basis')
    pairs=add_fields(pairs,evidence,new_cols)
    by_pair={(r['record_key'],r['service']):r for r in evidence}
    gold=pq.read_table(V2/'recommendation_candidates.parquet')
    kept=[by_pair[r['record_key'],r['service']]['eligible'] for r in gold.select(['record_key','service']).to_pylist()]
    gold=gold.filter(pa.array(kept))
    new_e=[by_pair[r['record_key'],r['service']] for r in gold.select(['record_key','service']).to_pylist()]
    gold=add_fields(gold,new_e,new_cols)
    for field in ('phone_status','website_status','phone_flags','website_flags'):
        gold=gold.append_column(field,pa.array([clean_contacts[r['record_key']][field] for r in new_e],type=contacts.field(field).type))
    for dest,state,src in [('display_phone','phone_status','official_phone_e164'),('display_website_host','website_status','official_web_host')]:
        vals=[clean_contacts[r['record_key']] for r in new_e]
        gold=gold.append_column(dest,pa.array([r[src] if r[state]=='listed_unconfirmed' else None for r in vals],type=pa.string()))
    if len(gold)!=sum(r['eligible'] for r in evidence) or len(by_pair)!=len(evidence):
        raise ValueError('Candidate accounting failed')
    if sum(len(r['citations']) for r in evidence)!=len(quotes):
        raise ValueError('Citation accounting failed')
    # Build the verification queue from exact retained source items, including mixed records.
    review=pairs.filter(pa.array([not r['eligible'] and any(q['kind']=='direct' for q in r['citations']) for r in evidence]))
    places=pq.read_table(geo/'places').sort_by([('search_key','ascending'),('place_id','ascending')])
    output.mkdir(parents=True,exist_ok=False)
    tables=dict(clean_facilities=clean, service_evidence=pairs,recommendation_candidates=gold,
                service_review_queue=review, citation_attribution=pa.Table.from_pylist(quotes),place_lookup=places)
    metrics={r['metric']:r['value'] for r in pq.read_table(V2/'build_summary.parquet').to_pylist()}
    metrics.update(identity_review_records=sum(r['identity_review'] for r in records),
                   location_review_records=sum(r['location_review'] for r in records),
                   candidate_facility_service_pairs=len(gold),distinct_candidate_facilities=len(set(gold['record_key'].to_pylist())),
                   eligible_pairs_held_since_v2=len(kept)-sum(kept),
                   cited_items=len(quotes),direct_pairs_held_for_review=len(review),place_lookup_rows=len(places),
                   multi_record_duplicate_groups=len({x['duplicate_group_id'] for x in id_rows if x['duplicate_group_size']>1}))
    for service,total in Counter(gold['service'].to_pylist()).items():
        metrics['candidate_pairs_'+service]=total
    import datetime
    tables['build_summary']=pa.Table.from_pylist([dict(metric=k,value=v,as_of=datetime.date(2026,11,21))
                                                  for k,v in sorted(metrics.items())],schema=pq.read_schema(V2/'build_summary.parquet'))
    for name,table in tables.items():
        pq.write_table(table,output/(name+'.parquet'),compression='zstd')
    print(json.dumps(metrics,indent=2,sort_keys=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--geo-cache',type=Path,help='Existing audited Spark facilities/ and places/ outputs')
    args=parser.parse_args()
    if args.geo_cache:
        assemble(args.geo_cache,args.output)
    else:
        with tempfile.TemporaryDirectory() as folder:
            staging=Path(folder)
            write_geo(staging)
            assemble(staging,args.output)


if __name__=='__main__':
    main()
