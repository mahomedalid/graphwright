---
description: Example business graph topology for the synthetic retail SQLite fixture.
---

# Graph Topology: Retail Example

This example topology accompanies the synthetic SQLite fixture. Replace it with your own business graph; the product has no built-in entity types.

## Node Types

### Customer

A person or organization that buys products.

- Key: customer identifier
- Properties: name, email, city
- Synonyms: client, buyer

### Order

A purchase transaction placed by a customer at a store.

- Key: order number
- Properties: order date, total amount, status

### Product

An item that can be sold.

- Key: SKU
- Properties: name, category, list price

### Store

A physical or online location where orders are placed.

- Key: store code
- Properties: name, region

## Edge Types

### PLACED

A customer placed an order.

- From: Customer
- To: Order
- Cardinality: one-to-many

### CONTAINS

An order contains a product line.

- From: Order
- To: Product
- Cardinality: many-to-many
- Properties: quantity, unit price

### SOLD_AT

An order was sold at a store.

- From: Order
- To: Store
- Cardinality: many-to-one

## Constraints

- Every Order is placed by exactly one Customer.
- An Order is sold at no more than one Store.

## Hints

- Source systems may format the same identifier differently (prefixes, zero padding, letter case, whitespace).
- Backup, temporary, and audit tables are usually not authoritative sources.
