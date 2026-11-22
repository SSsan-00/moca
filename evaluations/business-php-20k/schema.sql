CREATE TABLE customers (tenant TEXT, id TEXT, tier TEXT, region TEXT, blocked INTEGER,
  tax_exempt INTEGER, credit_limit INTEGER, balance INTEGER, PRIMARY KEY(tenant,id));
CREATE TABLE products (tenant TEXT, sku TEXT, category TEXT, active INTEGER, price INTEGER, stock INTEGER,
  PRIMARY KEY(tenant,sku));
CREATE TABLE memberships (tenant TEXT, customer TEXT, program TEXT, expires TEXT, points INTEGER);
CREATE TABLE orders (tenant TEXT, id TEXT, customer TEXT, sku TEXT, quantity INTEGER, amount INTEGER,
  paid INTEGER, state TEXT, note TEXT, policy INTEGER, PRIMARY KEY(tenant,id));
CREATE TABLE ledger (tenant TEXT, customer TEXT, order_id TEXT, amount INTEGER, kind TEXT);
CREATE TABLE events (tenant TEXT, order_id TEXT, kind TEXT, amount INTEGER, at TEXT);
INSERT INTO customers VALUES
 ('east','gold','gold','island',0,0,500000,0),('east','silver','silver','mainland',0,1,NULL,0),
 ('east','blocked','gold','mainland',1,0,500000,0),('west','gold','gold','mainland',0,0,500000,77);
INSERT INTO products VALUES
 ('east','food','food',1,677,30),('east','regular','general',1,1801,50),
 ('east','inactive','general',0,620,6),('east','restricted','restricted',1,1403,15),
 ('east','expensive','general',1,3500000000,6),
 ('west','expensive','general',1,3500000001,6),('west','food','food',1,501,0);
WITH RECURSIVE n(id) AS (SELECT 0 UNION ALL SELECT id+1 FROM n WHERE id < 250)
INSERT INTO memberships SELECT 'east','gold', printf('club-%03d', id),'2026-11-20',550 FROM n;
