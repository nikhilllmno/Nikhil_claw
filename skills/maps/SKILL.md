---
name: maps
description: Find places, distances, driving routes, and time zones
version: 1.0.0
tags: [productivity, travel, maps]
requires_toolsets: [core]
---

# Maps

Use this skill when the user asks where a place is, how far two places are, how long a drive
takes, what is near a place, or what the local time is somewhere.

## Steps

1. Use the open data services of OpenStreetMap from the sandbox, with a short Python script
   and the `requests` package:
   - geocoding (address to coordinates and back): Nominatim,
     `https://nominatim.openstreetmap.org/search?format=jsonv2&q=<text>`,
   - driving routes, distance, and time: the public OSRM demo server,
     `https://router.project-osrm.org/route/v1/driving/<lon>,<lat>;<lon>,<lat>`. This server
     has car data only. For walking or cycling, give the straight-line distance and a map
     link, and say that it is not a route,
   - places near a point (cafes, pharmacies, stations): the Overpass API,
     `https://overpass-api.de/api/interpreter`,
   - the time zone of a point: the Python package `timezonefinder`, then `zoneinfo`.
2. Geocode each place first. When a name matches more than one place, show the top matches
   with their country and ask the user which one.
3. Give the result with units: kilometers or miles (as the user prefers), hours and minutes,
   and the time zone name.
4. Give a link that opens the result on a map, for example
   `https://www.openstreetmap.org/?mlat=<lat>&mlon=<lon>&zoom=15`.
5. Give the attribution "Data © OpenStreetMap contributors" and the link
   `https://www.openstreetmap.org/copyright` with each result.

## Rules

- Follow the usage policy of each service. For Nominatim
  (`https://operations.osmfoundation.org/policies/nominatim/`): send a `User-Agent` header
  that names the app, send at most one request each second, keep the results of a session and
  do not repeat a request, and do not send bulk requests.
- Do not send private or confidential data to the public services. Do not geocode the home
  address of a person, a private address from an email or a document, or a list of customer
  addresses. Ask the user for a public place nearby, or tell them to use a service that their
  company approved.
- Travel times are estimates without live traffic. Say so.
- Public transport times are not in OSRM. For them, give the link of the local transport
  service.
