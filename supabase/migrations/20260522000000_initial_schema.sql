-- Initial schema for linebot-config Supabase project
-- Reconstructed from application code on 2026-05-22

-- CC Bot 對話記憶
CREATE TABLE IF NOT EXISTS cc_conversations (
    id         bigserial PRIMARY KEY,
    user_id    text        NOT NULL,
    role       text        NOT NULL CHECK (role IN ('user', 'assistant')),
    content    text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cc_conversations_user_id_created_at
    ON cc_conversations (user_id, created_at DESC);

-- KT BIKER 排程任務設定
CREATE TABLE IF NOT EXISTS bot_schedules (
    task_name       text PRIMARY KEY,
    display_name    text        NOT NULL DEFAULT '',
    enabled         boolean     NOT NULL DEFAULT true,
    schedule_day    integer     NOT NULL DEFAULT 1,
    schedule_hour   integer     NOT NULL DEFAULT 9,
    schedule_minute integer     NOT NULL DEFAULT 0,
    content         text        NOT NULL DEFAULT '',
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- 本地 Daemon 任務佇列
CREATE TABLE IF NOT EXISTS pending_tasks (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    task_name  text        NOT NULL,
    status     text        NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'running', 'done', 'error')),
    params     jsonb,
    result     text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pending_tasks_status_created_at
    ON pending_tasks (status, created_at);
