---
description: Graph topology for this workspace. Replace the placeholders with your business entities and relationships.
---

# Graph Topology: New Workspace

Describe the business graph you want to build. With Foundry configured, you can use
plain text and run `ddg topology` or start an auto/Foundry discovery run to extract
and validate the graph. Without Foundry, use the structured format below.
Discovery requires at least one validated node type.

Add other `.md` files alongside this file or in subfolders for background context.
Their contents are unverified references, not topology definitions or approved facts.

Format reference (copy blocks out of this code fence and edit them):

```markdown
### Customer

A person or organization that buys products.

- Key: customer identifier
- Properties: name, email, city
- Synonyms: client, buyer

### PLACED

A customer placed an order.

- From: Customer
- To: Order
- Cardinality: one-to-many    (one-to-one | one-to-many | many-to-one | many-to-many)
- Properties: channel
```

## Node Types

## Edge Types

## Constraints

## Hints
