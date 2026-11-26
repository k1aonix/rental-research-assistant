import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


SCHEMA = '''
CREATE TABLE IF NOT EXISTS imports(import_id INTEGER PRIMARY KEY,version TEXT UNIQUE NOT NULL,source_manifest TEXT NOT NULL,frozen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS authors(author_key INTEGER PRIMARY KEY,import_id INTEGER NOT NULL REFERENCES imports,author_id TEXT NOT NULL,nickname TEXT,profile_text TEXT,history_titles TEXT NOT NULL,quality TEXT NOT NULL,UNIQUE(import_id,author_id));
CREATE TABLE IF NOT EXISTS posts(post_key INTEGER PRIMARY KEY,import_id INTEGER NOT NULL REFERENCES imports,note_id TEXT NOT NULL,author_key INTEGER REFERENCES authors,url TEXT NOT NULL,title TEXT,body TEXT,published_at TEXT,quality TEXT NOT NULL,community_refs TEXT NOT NULL,UNIQUE(import_id,note_id));
CREATE TABLE IF NOT EXISTS comments(comment_key INTEGER PRIMARY KEY,post_key INTEGER NOT NULL REFERENCES posts,comment_id TEXT NOT NULL,parent_key INTEGER REFERENCES comments,level INTEGER NOT NULL CHECK(level IN(1,2)),fetch_order INTEGER NOT NULL CHECK(fetch_order BETWEEN 1 AND 10),body TEXT NOT NULL,UNIQUE(post_key,comment_id),UNIQUE(post_key,fetch_order));
CREATE TABLE IF NOT EXISTS searches(search_key INTEGER PRIMARY KEY,import_id INTEGER NOT NULL REFERENCES imports,query_text TEXT NOT NULL,region TEXT NOT NULL,station_poi TEXT,source_order INTEGER NOT NULL,quality TEXT NOT NULL,UNIQUE(import_id,source_order));
CREATE TABLE IF NOT EXISTS search_results(result_key INTEGER PRIMARY KEY,search_key INTEGER NOT NULL REFERENCES searches,position INTEGER NOT NULL CHECK(position>0),raw_note_id TEXT,raw_url TEXT,raw_title TEXT,raw_summary TEXT,quality TEXT NOT NULL,UNIQUE(search_key,position));
CREATE TABLE IF NOT EXISTS profiles(profile_id TEXT PRIMARY KEY,owner_hash TEXT UNIQUE NOT NULL,import_id INTEGER REFERENCES imports,state TEXT NOT NULL CHECK(state IN('draft','locked')),min_rent_cent INTEGER,max_rent_cent INTEGER,commute_limit_sec INTEGER,walk_limit_sec INTEGER,target_snapshot TEXT NOT NULL,target_confirmed_at TEXT,created_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS preferences(preference_id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES profiles ON DELETE CASCADE,kind TEXT NOT NULL,original_text TEXT NOT NULL,requirement TEXT NOT NULL CHECK(requirement IN('must','flexible')),display_order INTEGER NOT NULL,UNIQUE(profile_id,display_order));
CREATE TABLE IF NOT EXISTS records(record_id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES profiles ON DELETE CASCADE,post_key INTEGER NOT NULL REFERENCES posts,list_status TEXT CHECK(list_status IN('candidate','screened','archived')),community_supplement TEXT,manual_rent_cent INTEGER,manual_monthly_cent INTEGER,manual_initial_cent INTEGER,manual_commute_sec INTEGER,remark TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,candidate_added_at TEXT,version INTEGER NOT NULL DEFAULT 1,UNIQUE(profile_id,post_key));
CREATE TABLE IF NOT EXISTS record_manual_adjustments(record_id TEXT PRIMARY KEY REFERENCES records ON DELETE CASCADE,walk_sec INTEGER CHECK(walk_sec IS NULL OR walk_sec>=0),preference_results TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS record_time_overrides(record_id TEXT PRIMARY KEY REFERENCES records ON DELETE CASCADE,commute_overridden INTEGER NOT NULL DEFAULT 0,walk_overridden INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES profiles ON DELETE CASCADE,record_id TEXT REFERENCES records ON DELETE CASCADE,mode TEXT NOT NULL,state TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,progress TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,queued_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(profile_id,mode,idempotency_key));
CREATE TABLE IF NOT EXISTS steps(step_id TEXT PRIMARY KEY,record_id TEXT NOT NULL REFERENCES records ON DELETE CASCADE,step_name TEXT NOT NULL,scope_key TEXT NOT NULL,state TEXT NOT NULL,input_refs TEXT NOT NULL,input_hash TEXT NOT NULL,result TEXT,model_version TEXT,prompt_version TEXT,attempt_log TEXT NOT NULL,UNIQUE(record_id,step_name,scope_key));
CREATE TABLE IF NOT EXISTS evidence(evidence_id TEXT PRIMARY KEY,record_id TEXT NOT NULL REFERENCES records ON DELETE CASCADE,post_key INTEGER REFERENCES posts,comment_key INTEGER REFERENCES comments,author_key INTEGER REFERENCES authors,evidence_key TEXT NOT NULL,locator TEXT NOT NULL,excerpt TEXT NOT NULL,uses TEXT NOT NULL,UNIQUE(record_id,evidence_key));
CREATE TABLE IF NOT EXISTS conditions(condition_id TEXT PRIMARY KEY,record_id TEXT NOT NULL REFERENCES records ON DELETE CASCADE,preference_id TEXT REFERENCES preferences ON DELETE CASCADE,condition_code TEXT NOT NULL,result TEXT NOT NULL CHECK(result IN('pass','fail','unknown')),evidence_ids TEXT NOT NULL,route_context TEXT,decision TEXT CHECK(decision IN('continue','abort')),decided_at TEXT,version INTEGER NOT NULL DEFAULT 1,UNIQUE(record_id,condition_code));
CREATE TABLE IF NOT EXISTS reports(report_id TEXT PRIMARY KEY,record_id TEXT NOT NULL REFERENCES records ON DELETE CASCADE,kind TEXT NOT NULL CHECK(kind IN('brief','full')),content TEXT NOT NULL,input_hash TEXT NOT NULL,generated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_full_report ON reports(record_id) WHERE kind='full';
CREATE TABLE IF NOT EXISTS viewing(record_id TEXT PRIMARY KEY REFERENCES records ON DELETE CASCADE,conclusion TEXT NOT NULL,summary TEXT NOT NULL,followup TEXT NOT NULL,general_checks TEXT NOT NULL,risk_checks TEXT NOT NULL,version INTEGER NOT NULL,saved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS command_receipts(owner_hash TEXT NOT NULL,operation TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,PRIMARY KEY(owner_hash,operation,idempotency_key));
CREATE TRIGGER IF NOT EXISTS author_batch BEFORE INSERT ON posts WHEN NEW.author_key IS NOT NULL AND NOT EXISTS(SELECT 1 FROM authors a WHERE a.author_key=NEW.author_key AND a.import_id=NEW.import_id) BEGIN SELECT RAISE(ABORT,'author batch mismatch'); END;
CREATE TRIGGER IF NOT EXISTS record_batch BEFORE INSERT ON records WHEN NOT EXISTS(SELECT 1 FROM profiles p JOIN posts s ON s.import_id=p.import_id WHERE p.profile_id=NEW.profile_id AND s.post_key=NEW.post_key) BEGIN SELECT RAISE(ABORT,'record batch mismatch'); END;
CREATE TRIGGER IF NOT EXISTS comment_parent BEFORE UPDATE OF parent_key ON comments WHEN NEW.parent_key IS NOT NULL AND NOT EXISTS(SELECT 1 FROM comments c WHERE c.comment_key=NEW.parent_key AND c.post_key=NEW.post_key AND c.level=1) BEGIN SELECT RAISE(ABORT,'comment parent mismatch'); END;
CREATE TRIGGER IF NOT EXISTS condition_profile BEFORE INSERT ON conditions WHEN NEW.preference_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM records r JOIN preferences p ON p.profile_id=r.profile_id WHERE r.record_id=NEW.record_id AND p.preference_id=NEW.preference_id) BEGIN SELECT RAISE(ABORT,'condition profile mismatch'); END;
PRAGMA user_version=137;
'''


class Database:
    def __init__(self, filename):
        if filename != ':memory:':
            Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(filename, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('PRAGMA busy_timeout=5000')
        if filename != ':memory:':
            self.connection.execute('PRAGMA journal_mode=WAL')
        # An old unrelated database is never silently relabeled as a new schema.
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 137):
            self.connection.close()
            raise RuntimeError('数据库版本不兼容，请先备份并执行明确的迁移，禁止自动套用新步行语义。')
        known = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='profiles'").fetchone()
        if known and 'walk_limit_sec' not in {row[1] for row in self.connection.execute('PRAGMA table_info(profiles)')}:
            self.connection.close()
            raise RuntimeError('检测到旧画像表；禁止将到站步行值静默迁为累计步行上限。')
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                yield self.connection
                self.connection.execute('COMMIT')
            except BaseException:
                self.connection.execute('ROLLBACK')
                raise

    def one(self, sql, args=()):
        with self.lock:
            row = self.connection.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.connection.execute(sql, args).fetchall()]

    def close(self):
        self.connection.close()
