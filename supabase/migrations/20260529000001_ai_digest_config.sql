-- AI 日報排程設定表（singleton，只有 id=1 這一列）
CREATE TABLE IF NOT EXISTS ai_digest_config (
    id      integer PRIMARY KEY DEFAULT 1,
    enabled boolean NOT NULL DEFAULT true,
    mode    text    NOT NULL DEFAULT 'daily',   -- 'daily' | 'weekly'
    hour    integer NOT NULL DEFAULT 9,
    minute  integer NOT NULL DEFAULT 0,
    weekday text    NOT NULL DEFAULT 'mon',     -- mon|tue|wed|thu|fri|sat|sun
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 確保只有 id=1
ALTER TABLE ai_digest_config ADD CONSTRAINT ai_digest_config_singleton CHECK (id = 1);

-- 插入預設設定（每天 09:00）
INSERT INTO ai_digest_config (id, enabled, mode, hour, minute, weekday)
VALUES (1, true, 'daily', 9, 0, 'mon')
ON CONFLICT (id) DO NOTHING;

-- RLS
ALTER TABLE ai_digest_config ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon select" ON ai_digest_config FOR SELECT USING (true);
CREATE POLICY "anon update" ON ai_digest_config FOR UPDATE USING (true);
CREATE POLICY "anon insert" ON ai_digest_config FOR INSERT WITH CHECK (true);
