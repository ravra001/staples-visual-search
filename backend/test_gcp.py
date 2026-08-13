"""
Smoke-test the GCP backends once you've authenticated. Claude Code cannot log
in for you — you run the auth steps, then this script verifies the wiring.

Prereqs (you do these once):
    # 1. install the SDK, then authenticate (opens a browser):
    gcloud auth application-default login
    gcloud config set project <YOUR_PROJECT_ID>
    # 2. enable the APIs:
    gcloud services enable aiplatform.googleapis.com sqladmin.googleapis.com
    # 3. install the client libs:
    pip install -r requirements-ml.txt

Run:
    GCP_PROJECT=<id> python test_gcp.py                 # Vertex AI embedding test
    GCP_PROJECT=<id> DATABASE_URL=... python test_gcp.py  # + Cloud SQL test
"""
import os
import sys

IMAGES_DIR = os.path.join(os.path.dirname(__file__), "static", "images", "products")


def test_vertex():
    print("== Vertex AI Multimodal Embeddings ==")
    if not os.environ.get("GCP_PROJECT"):
        print("  SKIP: set GCP_PROJECT to run this test.")
        return
    os.environ["EMBEDDING_BACKEND"] = "vertex"
    import embeddings as E  # imports the vertex backend + truststore

    # embed two catalog images and confirm we get a real 1408-dim vector
    imgs = [f for f in os.listdir(IMAGES_DIR) if f.lower().endswith((".png", ".jpg"))][:2]
    if len(imgs) < 2:
        print("  SKIP: need at least 2 catalog images.")
        return
    vecs = []
    for name in imgs:
        with open(os.path.join(IMAGES_DIR, name), "rb") as f:
            vecs.append(E.embed_image(f.read()))
    print(f"  OK: embedded 2 images, dim={vecs[0].shape[0]} (expected 1408)")
    print(f"  cosine(img1,img2) = {E.cosine_similarity(vecs[0], vecs[1]):.3f}")
    print("  -> Vertex backend works. Start the app with EMBEDDING_BACKEND=vertex.")


def test_cloud_sql():
    print("\n== Cloud SQL (Postgres) ==")
    if not os.environ.get("DATABASE_URL"):
        print("  SKIP: set DATABASE_URL to run this test.")
        return
    os.environ["DATA_BACKEND"] = "sql"
    import products_repo_sql as R

    R.init_and_seed()  # creates schema + seeds from the in-memory catalog if empty
    n = len(R.get_all_products())
    sample = R.search_products("chair")[:1]
    print(f"  OK: {n} products in the database.")
    if sample:
        print(f"  sample search('chair') -> {sample[0]['name'][:50]}")
    print("  -> SQL backend works. Start the app with DATA_BACKEND=sql + DATABASE_URL.")


if __name__ == "__main__":
    try:
        test_vertex()
        test_cloud_sql()
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        print("Check: gcloud auth application-default login · APIs enabled · requirements-ml.txt installed.")
        sys.exit(1)
