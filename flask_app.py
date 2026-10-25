"""DogForU: find the right dog breed for you and nearby places to adopt one."""

import ipaddress
import json
import os
from pathlib import Path

import petpy
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_json(filename):
    with open(DATA_DIR / filename, encoding="utf-8") as f:
        return json.load(f)


# breed name -> {"temperament": [...], "bred_for": str, "life_span": str, "image": url}
BREEDS = load_json("breeds.json")
POPULAR_DOGS = load_json("popular_dogs.json")    # breed name -> image url
REGION_CODES = load_json("region_codes.json")    # "British Columbia" -> "BC"

BREED_IMAGES = {name: info["image"] for name, info in BREEDS.items()}
BREEDS_BY_LOWER_NAME = {name.lower(): name for name in BREEDS}

# Checkbox options shown on the Find A Dog page
TEMPERAMENT_OPTIONS = ["Intelligent", "Fierce", "Friendly", "Alert", "Loyal",
                       "Energetic", "Sociable", "Playful", "Adventurous", "Active"]
BRED_FOR_OPTIONS = ["Hunting", "Guarding", "Watchdog", "Companion",
                    "Retrieving", "Lapdog", "Racing"]


# ---------------------------------------------------------------------------
# External APIs (keys come from environment variables, see .env.example)
# ---------------------------------------------------------------------------

PETFINDER_KEY = os.getenv("PETFINDER_KEY")
PETFINDER_SECRET = os.getenv("PETFINDER_SECRET")
IPINFO_TOKEN = os.getenv("IPINFO_TOKEN")

_petfinder = None


def get_petfinder():
    """Create the Petfinder client on first use, or return None if no keys are set."""
    global _petfinder
    if _petfinder is None and PETFINDER_KEY and PETFINDER_SECRET:
        _petfinder = petpy.Petfinder(key=PETFINDER_KEY, secret=PETFINDER_SECRET)
    return _petfinder


def visitor_ip():
    """The visitor's public IP, or None when running locally."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = forwarded.split(",")[0].strip() or request.remote_addr
    try:
        if ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback:
            return None
    except ValueError:
        return None
    return ip


def visitor_location():
    """Look up the visitor's city, region and country with ipinfo.io."""
    ip = visitor_ip()
    url = f"https://ipinfo.io/{ip}/json" if ip else "https://ipinfo.io/json"
    params = {"token": IPINFO_TOKEN} if IPINFO_TOKEN else {}
    response = requests.get(url, params=params, timeout=5)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Breed search and matching
# ---------------------------------------------------------------------------

def find_breed(name):
    """Return the breed's canonical name for an exact (case-insensitive) match."""
    return BREEDS_BY_LOWER_NAME.get(name.strip().lower())


def related_by_temperament(breed):
    """Other breeds that share at least one temperament trait."""
    traits = set(BREEDS[breed]["temperament"])
    return {other: info["image"] for other, info in BREEDS.items()
            if other != breed and traits & set(info["temperament"])}


def related_by_name(breed):
    """Other breeds that share a word in their name (e.g. other Collies)."""
    words = breed.split()
    return {other: image for other, image in BREED_IMAGES.items()
            if other != breed and (breed in other or any(w in other for w in words))}


def related_breeds(breed):
    return related_by_name(breed) if len(breed.split()) > 1 else related_by_temperament(breed)


def render_breed_page(breed, related):
    info = BREEDS[breed]
    return render_template(
        "doginfo.html",
        dogbreed=breed,
        dogresults=related,
        dogfor=info["bred_for"],
        dogtemperament=info["temperament"],
        doglife=info["life_span"],
        dogimage=info["image"],
    )


def handle_search():
    """Handle the navbar search form, which every page posts back to itself.

    Returns a response for a search, or None so the page renders normally.
    """
    if request.method != "POST" or "search" not in request.form:
        return None

    query = " ".join(request.form["search"].split())
    if len(query) < 2:
        return None

    breed = find_breed(query)
    if breed:
        return render_breed_page(breed, related_breeds(breed))

    matches = {name: image for name, image in BREED_IMAGES.items()
               if query.lower() in name.lower()}
    if matches:
        return render_template("results.html", dogresults=matches, name=query)
    return render_template("redirect.html")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
@app.route("/home", methods=["GET", "POST"])
def index():
    return handle_search() or render_template("index.html")


@app.route("/dogs", methods=["GET", "POST"])
def popular_dogs():
    return handle_search() or render_template("dogs.html", populardogs=POPULAR_DOGS)


@app.route("/alldogs", methods=["GET", "POST"])
def all_dogs():
    return handle_search() or render_template("alldogs.html", dogs=BREED_IMAGES)


@app.route("/about", methods=["GET", "POST"])
def about():
    return handle_search() or render_template("about.html")


@app.route("/doginfo/<name>", methods=["GET", "POST"])
def dog_info(name):
    search_result = handle_search()
    if search_result:
        return search_result

    breed = find_breed(name)
    if not breed:
        return render_template("redirect.html"), 404
    return render_breed_page(breed, related_by_temperament(breed))


@app.route("/dogfinder", methods=["GET", "POST"])
def dog_finder():
    """Filter breeds by the temperament and bred-for checkboxes."""
    search_result = handle_search()
    if search_result:
        return search_result

    wanted_traits = request.form.getlist("tcb[]")
    wanted_purposes = request.form.getlist("fcb[]")
    submitted = request.method == "POST" and bool(wanted_traits or wanted_purposes)

    results = {}
    if submitted:
        for breed, info in BREEDS.items():
            has_traits = all(t in info["temperament"] for t in wanted_traits)
            has_purposes = all(p in info["bred_for"] for p in wanted_purposes)
            if has_traits and has_purposes:
                results[breed] = info["image"]

    return render_template(
        "dogfinder.html",
        acc=1 if submitted else 0,
        lendogresults=len(results),
        dogresults=results,
        dogimages=BREED_IMAGES,
        temperaments=TEMPERAMENT_OPTIONS,
        dogfor=BRED_FOR_OPTIONS,
    )


@app.route("/adopt", methods=["GET", "POST"])
def adopt():
    """List adoption organizations within 100 miles of the visitor (US and Canada)."""
    search_result = handle_search()
    if search_result:
        return search_result

    try:
        petfinder = get_petfinder()  # authenticates with Petfinder on first use
        if petfinder is None:
            app.logger.warning("PETFINDER_KEY / PETFINDER_SECRET not set; adoption search disabled")
            return render_template("aredirect.html")

        location = visitor_location()
        country = location.get("country")
        region = REGION_CODES.get(location.get("region", ""))
        if country not in ("US", "CA") or not region:
            return render_template("aredirect.html")

        organizations = petfinder.organizations(
            location=f"{location['city']}, {region}", distance=100, sort="distance"
        )
    except Exception:
        app.logger.exception("Adoption lookup failed")
        return render_template("aredirect.html")

    return render_template("adopt.html", organizations=organizations, country=country)


# PythonAnywhere's default WSGI config imports `flask_app`; keep that name working.
flask_app = DogForU = app

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
