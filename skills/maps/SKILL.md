---
name: maps
description: Find places, distances, routes, and time zones
version: 1.0.0
tags: [productivity, travel, maps]
requires_toolsets: [core]
---

# Maps

Use this skill when the user asks where a place is, how far two places are, how long a trip
takes, what is near a place, or what the local time is somewhere.

## Steps

1. Use the open data services of OpenStreetMap from the sandbox, with a short Python script
   and the `requests` package:
   - geocoding (address to coordinates and back): Nominatim,
     `https://nominatim.openstreetmap.org/search?format=jsonv2&q=<text>`,
   - routes, distance, and travel time by car, bike, or foot: the public OSRM server,
     `https://router.project-osrm.org/route/v1/driving/<lon>,<lat>;<lon>,<lat>`,
   - places near a point (cafes, pharmacies, stations): the Overpass API,
     `https://overpass-api.de/api/interpreter`,
   - the time zone of a point: the Python package `timezonefinder`, then `zoneinfo`.
2. Geocode each place first. When a name matches more than one place, show the top matches
   with their country and ask the user which one.
3. Give the result with units: kilometers or miles (as the user prefers), hours and minutes,
   and the time zone name.
4. Give a link that opens the result on a map, for example
   `https://www.openstreetmap.org/?mlat=<lat>&mlon=<lon>&zoom=15`.

## Rules

- Send a `User-Agent` header that names the app, and send at most one Nominatim request each
  second. These are the usage rules of the free services.
- Travel times are estimates without live traffic. Say so.
- Public transport times are not in OSRM. For them, give the link of the local transport
  service.
- Do not store the home address of the user unless the user asks.
