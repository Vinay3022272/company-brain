# pyrefly: ignore [missing-import]
from falkordb import FalkorDB

# Connect to your local FalkorDB instance
db = FalkorDB(host="localhost", port=6379)

# Create/select your project's graph
graph = db.select_graph("LLMWiki")

print("Connected to FalkorDB!")

graph.query("""
CREATE
  (db:Technology {
    id: 'falkordb',
    name: 'FalkorDB',
    description: 'A graph database for connected information'
  }),
  (kg:Concept {
    id: 'knowledge_graph',
    name: 'Knowledge Graph',
    description: 'A network of entities and relationships'
  }),
  (agent:Concept {
    id: 'ai_agent',
    name: 'AI Agent',
    description: 'A system that uses context to perform tasks'
  }),
  (wiki:WikiPage {
    id: 'llm_wiki',
    title: 'LLM Wiki',
    content: 'A structured knowledge base designed for AI agents'
  }),
  (db)-[:IMPLEMENTS]->(kg),
  (kg)-[:PROVIDES_CONTEXT_TO]->(agent),
  (wiki)-[:SUPPORTS]->(agent)
""")

print("Graph created!")