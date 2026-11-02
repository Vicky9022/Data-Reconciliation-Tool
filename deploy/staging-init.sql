-- Disposable local fixture only; no production account uses these credentials.
CREATE SCHEMA source_data;
CREATE SCHEMA destination_data;
CREATE TABLE source_data.customers(customer_id text PRIMARY KEY,name text,balance numeric(18,2),joined date);
INSERT INTO source_data.customers SELECT 'C'||lpad(i::text,5,'0'),'Customer '||i,1000+i,DATE '2026-01-01' FROM generate_series(0,9999) AS i;
CREATE TABLE destination_data.customers AS SELECT * FROM source_data.customers WHERE customer_id>='C00050';
UPDATE destination_data.customers SET balance=balance+450 WHERE customer_id BETWEEN 'C00050' AND 'C00079';
INSERT INTO destination_data.customers SELECT * FROM source_data.customers WHERE customer_id BETWEEN 'C00080' AND 'C00099';
INSERT INTO destination_data.customers SELECT 'X'||lpad(i::text,3,'0'),'New customer',4100,DATE '2026-01-01' FROM generate_series(0,24) AS i;
CREATE USER recon_read PASSWORD 'local-read-password';
GRANT USAGE ON SCHEMA source_data,destination_data TO recon_read;
GRANT SELECT ON ALL TABLES IN SCHEMA source_data,destination_data TO recon_read;
CREATE USER recon_jobs PASSWORD 'local-job-password';
GRANT CONNECT ON DATABASE reconcile TO recon_jobs;
GRANT USAGE,CREATE ON SCHEMA public TO recon_jobs;
