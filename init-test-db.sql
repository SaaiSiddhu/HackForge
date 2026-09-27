-- Initialize disposable test database if it does not already exist
SELECT 'CREATE DATABASE hackforge_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'hackforge_test')\gexec
