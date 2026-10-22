"""External systems a repository talks to, detected from imports (never from running anything).
Each hit keeps the file and line of the import so a reader can check it."""

import re
from collections import defaultdict
from typing import Dict, List

# import root -> (system name, category)
KNOWN = {
    "pymilvus": ("Milvus", "vector database"), "opensearchpy": ("OpenSearch", "search / vector index"),
    "elasticsearch": ("Elasticsearch", "search index"), "redis": ("Redis", "cache"), "aioredis": ("Redis", "cache"),
    "ioredis": ("Redis", "cache"), "psycopg2": ("PostgreSQL", "relational database"), "asyncpg": ("PostgreSQL", "relational database"),
    "psycopg": ("PostgreSQL", "relational database"), "pg": ("PostgreSQL", "relational database"),
    "pymysql": ("MySQL", "relational database"), "mysql": ("MySQL", "relational database"), "mysql2": ("MySQL", "relational database"),
    "sqlalchemy": ("SQL database (via SQLAlchemy)", "relational database"), "sqlite3": ("SQLite", "relational database"),
    "pymongo": ("MongoDB", "document database"), "motor": ("MongoDB", "document database"), "mongoose": ("MongoDB", "document database"),
    "boto3": ("AWS", "cloud services"), "aioboto3": ("AWS", "cloud services"), "botocore": ("AWS", "cloud services"),
    "requests_aws": ("AWS (signed requests)", "cloud services"), "aws-sdk": ("AWS", "cloud services"),
    "kafka": ("Kafka", "message broker"), "confluent_kafka": ("Kafka", "message broker"), "aiokafka": ("Kafka", "message broker"),
    "kafkajs": ("Kafka", "message broker"), "pika": ("RabbitMQ", "message broker"), "amqplib": ("RabbitMQ", "message broker"),
    "celery": ("Celery workers", "task queue"), "kombu": ("message broker (kombu)", "message broker"),
    "sentence_transformers": ("SentenceTransformers model", "ML model"), "transformers": ("Hugging Face model", "ML model"),
    "openai": ("OpenAI API", "external API"), "anthropic": ("Anthropic API", "external API"),
    "stripe": ("Stripe", "external API"), "twilio": ("Twilio", "external API"), "firebase_admin": ("Firebase", "external API"),
    "requests": ("outbound HTTP", "external API"), "httpx": ("outbound HTTP", "external API"), "aiohttp": ("outbound HTTP", "external API"),
    "axios": ("outbound HTTP", "external API"), "grpc": ("gRPC services", "external API"), "smtplib": ("SMTP mail server", "external API"),
    "sentry_sdk": ("Sentry", "observability"), "prometheus_client": ("Prometheus", "observability"),
    "datadog": ("Datadog", "observability"), "newrelic": ("New Relic", "observability"),
}


def detect(analyses: Dict[str, "FileAnalysis"], component_of) -> List[dict]:
    hits: Dict[str, dict] = {}
    for path, fa in sorted(analyses.items()):
        for imp, line in fa.import_lines.items():
            root = imp.split(".")[0] if not imp.startswith("@") else "/".join(imp.split("/")[:2])
            if root not in KNOWN:
                continue
            name, cat = KNOWN[root]
            h = hits.setdefault(name, {"system": name, "category": cat, "packages": set(), "uses": [], "components": set()})
            h["packages"].add(root)
            h["components"].add(component_of(path))
            if not any(u["file"] == path for u in h["uses"]):
                h["uses"].append({"file": path, "line": line})
    out = []
    for h in hits.values():
        out.append({**h, "packages": sorted(h["packages"]), "components": sorted(h["components"]), "uses": h["uses"][:8],
                    "use_count": len(h["uses"])})
    return sorted(out, key=lambda h: (-h["use_count"], h["system"]))


def details_from_text(text_by_file: Dict[str, str], found: List[dict]) -> Dict[str, List[str]]:
    """Cheap extras that make an integration concrete: model names, AWS services, DB dialects, hostnames in URL literals."""
    extra = defaultdict(set)
    for path, text in text_by_file.items():
        for m in re.finditer(r"SentenceTransformer\(\s*['\"]([^'\"]+)['\"]", text):
            extra["SentenceTransformers model"].add(f"model `{m.group(1)}` ({path})")
        for m in re.finditer(r"boto3\.(?:client|resource)\(\s*['\"]([a-z0-9-]+)['\"]", text):
            extra["AWS"].add(f"service `{m.group(1)}` ({path})")
        for m in re.finditer(r"['\"](postgresql|postgres|mysql|sqlite|mongodb)(?:\+\w+)?://", text):
            extra["SQL database (via SQLAlchemy)" if m.group(1) in ("postgresql", "postgres", "mysql", "sqlite") else "MongoDB"].add(f"dialect `{m.group(1)}` ({path})")
    return {k: sorted(v)[:6] for k, v in extra.items()}
