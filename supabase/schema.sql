CREATE TABLE IF NOT EXISTS public.users (
    id text PRIMARY KEY,
    email text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role text NOT NULL CONSTRAINT users_role_check
        CHECK (role IN ('citizen', 'editor', 'admin')),
    created_at text NOT NULL
);

CREATE TABLE IF NOT EXISTS public.reports (
    id text PRIMARY KEY,
    reporter_id text NOT NULL REFERENCES public.users(id),
    title text NOT NULL,
    body text NOT NULL,
    category text NOT NULL,
    location text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    editor_note text NOT NULL DEFAULT '',
    editor_id text REFERENCES public.users(id),
    media_name text,
    media_type text,
    media_path text,
    created_at text NOT NULL,
    updated_at text NOT NULL
);

CREATE INDEX IF NOT EXISTS reports_status_created
    ON public.reports(status, created_at DESC);
CREATE INDEX IF NOT EXISTS reports_reporter_created
    ON public.reports(reporter_id, created_at DESC);

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reports ENABLE ROW LEVEL SECURITY;

GRANT USAGE ON SCHEMA public TO service_role;
GRANT ALL ON public.users, public.reports TO service_role;
