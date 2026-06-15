-- Run this against DB-1 as the postgres superuser

-- Read-only user for push-router Lambda
CREATE USER push_readonly WITH PASSWORD 'choose-a-strong-password';
GRANT CONNECT ON DATABASE neuralops_db TO push_readonly;
GRANT USAGE ON SCHEMA public TO push_readonly;
GRANT SELECT ON device_tokens TO push_readonly;
GRANT SELECT ON users TO push_readonly;

-- Write user for push-dispatch Lambda (needs to write delivery log and update tokens)
CREATE USER push_writer WITH PASSWORD 'choose-a-strong-password';
GRANT CONNECT ON DATABASE neuralops_db TO push_writer;
GRANT USAGE ON SCHEMA public TO push_writer;
GRANT SELECT, INSERT ON push_delivery_log TO push_writer;
GRANT SELECT, UPDATE ON device_tokens TO push_writer;
