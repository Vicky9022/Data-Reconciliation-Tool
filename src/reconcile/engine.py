"""Disk-backed, deterministic reconciliation over immutable CSV snapshots."""
import csv
import hashlib
import json
import sqlite3
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext

ENGINE_VERSION = '2.0.0'
CATEGORIES = ('missing_destination', 'unexpected_destination', 'duplicate_source',
              'duplicate_destination', 'value_mismatch', 'invalid_value', 'invalid_key',
              'missing_column', 'unexpected_column', 'changed_type')

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

def validate_rules(config):
    if not isinstance(config, dict) or set(config) - {'keys', 'fields', 'version', 'schemas'}:
        raise ValueError('Rules require keys, fields, and an optional version.')
    if len(canonical(config).encode('utf-8'))>65536:
        raise ValueError('Rule specification exceeds 64 KiB.')
    keys = config.get('keys')
    if 'version' in config and (not isinstance(config['version'],str) or not 1<=len(config['version'])<=64):
        raise ValueError('Rule version must contain 1–64 characters.')
    if not isinstance(keys, list) or not keys or len(keys) != len(set(keys)) or any(not isinstance(k, str) or not k for k in keys):
        raise ValueError('Choose at least one distinct business key column.')
    fields = config.get('fields', {})
    schemas=config.get('schemas',{})
    if not isinstance(schemas,dict) or set(schemas)-{'source','destination'} or any(not isinstance(s,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in s.items()) for s in schemas.values()):
        raise ValueError('Schemas must map source/destination column names to declared type strings.')
    if not isinstance(fields, dict):
        raise ValueError('Field rules must be an object.')
    if len(fields)>200: raise ValueError('At most 200 field rules are supported.')
    for field, rule in fields.items():
        if not isinstance(rule, dict) or set(rule) - {'type', 'tolerance', 'trim', 'casefold', 'nullable', 'severity'}:
            raise ValueError(f'Unsupported rule for {field}.')
        if field in keys:
            raise ValueError('Key fields cannot have normalization or comparison rules.')
        if rule.get('type', 'text') not in ('text', 'numeric', 'date'):
            raise ValueError(f'Unsupported type for {field}.')
        if rule.get('severity', 'medium') not in ('low', 'medium', 'high', 'critical'):
            raise ValueError('Unknown severity.')
        for option in ('trim', 'casefold', 'nullable'):
            if option in rule and not isinstance(rule[option], bool):
                raise ValueError(f'{option} must be true or false.')
        try:
            tolerance = Decimal(str(rule.get('tolerance', 0)))
            if not tolerance.is_finite() or tolerance < 0 or tolerance > Decimal('1e12'):
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise ValueError('Tolerance must be a finite nonnegative number, at most 1e12.') from None
        if rule.get('type', 'text') == 'text' and tolerance:
            raise ValueError('Text fields do not support numeric tolerances.')
    return config

def parsed(value, rule):
    if value is None or value == '':
        if rule.get('nullable', True):
            return None
        raise ValueError('Required value is empty')
    if rule.get('trim'):
        value = value.strip()
    if rule.get('casefold'):
        value = value.casefold()
    mode = rule.get('type', 'text')
    if mode == 'numeric':
        try:
            result = Decimal(value)
            if not result.is_finite():
                raise ValueError('Nonfinite numeric value')
            if len(result.as_tuple().digits)>1000 or abs(result.as_tuple().exponent)>1000:
                raise ValueError('Numeric value exceeds supported precision.')
            return result
        except InvalidOperation:
            raise ValueError('Invalid numeric value') from None
    if mode == 'date':
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value

def compare(a, b, rule):
    x, y = parsed(a, rule), parsed(b, rule)
    if x is None or y is None:
        return x == y
    tolerance = Decimal(str(rule.get('tolerance', 0)))
    if rule.get('type') == 'numeric':
        with localcontext() as context:
            context.prec=4000
            return abs(x - y) <= tolerance
    if rule.get('type') == 'date':
        # Mixing aware and naive timestamps is an explicit invalid-value exception.
        return Decimal(str(abs((x - y).total_seconds()))) <= tolerance * 86400
    return x == y

def reconcile(source_path, destination_path, config, staging_path, emit, progress=None):
    """Bound memory by staging keys and rows on disk; emit each exception immediately.

    emit receives a dictionary. SQLite temporary state contains input values and must
    be stored with the same access controls as the original snapshots.
    """
    validate_rules(config)
    db = sqlite3.connect(staging_path)
    db.execute('PRAGMA journal_mode=OFF')
    db.execute('CREATE TABLE rows(side INTEGER, key TEXT, line INTEGER, payload TEXT)')
    counts = [0, 0]
    columns = []
    issue_count = 0
    affected_keys = 0
    category_counts = {}
    def issue(category, key='', column='', source=None, destination=None, severity='high', detail=''):
        nonlocal issue_count
        issue_count += 1
        category_counts[category] = category_counts.get(category, 0) + 1
        emit(dict(category=category, key=key, column=column, source=source,
                  destination=destination, severity=severity, detail=detail))
    try:
        for side, path in enumerate((source_path, destination_path)):
            with open(path, encoding='utf-8-sig', newline='') as file:
                reader = csv.DictReader(file)
                header = reader.fieldnames
                if not header or len(set(header)) != len(header) or any(not c or len(c)>200 for c in header):
                    raise ValueError('CSV requires distinct, nonempty headers of at most 200 characters.')
                if len(header)>200:
                    raise ValueError('A dataset may contain at most 200 columns.')
                if any(k not in header for k in config['keys']):
                    raise ValueError('All business key columns must exist in both datasets.')
                columns.append(header)
                for line, row in enumerate(reader, 2):
                    counts[side] += 1
                    if counts[side]>int(os.environ.get('RECON_MAX_RECORDS','5000000')): raise ValueError('Snapshot exceeds the configured record limit.')
                    if None in row or any(v is None for v in row.values()):
                        raise ValueError(f'Malformed CSV at record {line}, side {side + 1}.')
                    key = [row[k] for k in config['keys']]
                    if any(v == '' for v in key):
                        issue('invalid_key', f'{"source" if side==0 else "destination"}:record:{line}',
                              source=row if side==0 else None, destination=row if side==1 else None)
                        continue
                    db.execute('INSERT INTO rows VALUES(?,?,?,?)', (side, canonical(key), line, canonical(row)))
                    if counts[side] % 5000 == 0:
                        db.commit()
                        if progress: progress('Indexing source' if side == 0 else 'Indexing destination', sum(counts))
            db.commit()
        db.execute('CREATE INDEX row_lookup ON rows(key,side)')
        source_columns, destination_columns = map(set, columns)
        unknown = set(config.get('fields', {})) - (source_columns | destination_columns)
        if unknown:
            raise ValueError('Rules refer to absent columns: ' + ', '.join(sorted(unknown)))
        for field in sorted(source_columns - destination_columns):
            issue('missing_column', column=field, source='present', destination='absent', severity='critical')
        for field in sorted(destination_columns - source_columns):
            issue('unexpected_column', column=field, source='absent', destination='present', severity='medium')
        schemas=config.get('schemas',{})
        for side,header in zip(('source','destination'),columns):
            if set(schemas.get(side,{}))-set(header): raise ValueError('Declared schema contains columns absent from its snapshot.')
        for field in sorted(set(schemas.get('source',{})) & set(schemas.get('destination',{}))):
            a,b=schemas['source'][field],schemas['destination'][field]
            if a!=b: issue('changed_type',column=field,source=a,destination=b,severity='critical',detail='Declared source and destination types differ. Explicit compatibility review required.')
        matched = 0
        total_keys = db.execute('SELECT COUNT(DISTINCT key) FROM rows').fetchone()[0]
        for key_index, (key, ns, nd) in enumerate(db.execute('SELECT key,SUM(side=0),SUM(side=1) FROM rows GROUP BY key ORDER BY key')):
            before = issue_count
            samples = []
            for side, count in enumerate((ns, nd)):
                records = [json.loads(r[0]) for r in db.execute('SELECT payload FROM rows WHERE key=? AND side=? ORDER BY line LIMIT 20', (key, side))]
                samples.append(records)
                if count > 1:
                    issue('duplicate_source' if side==0 else 'duplicate_destination', key,
                          source=records if side==0 else None, destination=records if side==1 else None,
                          detail=f'{count} rows share this key. Evidence shows the first 20; full input is retained.')
            a, b = samples
            if not ns:
                issue('unexpected_destination', key, destination=b)
            elif not nd:
                issue('missing_destination', key, source=a)
            elif ns == nd == 1:
                for field in sorted((source_columns & destination_columns) - set(config['keys'])):
                    rule = config.get('fields', {}).get(field, {})
                    try:
                        equal = compare(a[0][field], b[0][field], rule)
                        if not equal:
                            issue('value_mismatch', key, field, a[0][field], b[0][field], rule.get('severity', 'medium'))
                    except (ValueError, TypeError, OverflowError, InvalidOperation):
                        issue('invalid_value', key, field, a[0][field], b[0][field], rule.get('severity', 'high'),
                              'Value does not satisfy its declared field rule, or timestamps have incompatible timezone awareness.')
                if before == issue_count: matched += 1
            if before != issue_count: affected_keys += 1
            if progress and key_index % 1000 == 0: progress('Comparing records', key_index)
        def digest(path):
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                for block in iter(lambda:f.read(1024*1024), b''): h.update(block)
            return h.hexdigest()
        hashes = [digest(source_path), digest(destination_path)]
        fingerprint = hashlib.sha256(canonical([ENGINE_VERSION, hashes, config]).encode()).hexdigest()
        return dict(source_rows=counts[0], destination_rows=counts[1], matching_records=matched,
                    distinct_keys=total_keys, affected_keys=affected_keys, exception_count=issue_count,
                    categories=category_counts, source_sha256=hashes[0], destination_sha256=hashes[1],
                    fingerprint=fingerprint, engine=ENGINE_VERSION,
                    source_columns=columns[0], destination_columns=columns[1])
    finally:
        db.close()
