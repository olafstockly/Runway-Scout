RUNWAY SCOUT
Automated land screener for private GA airstrips (Cessna 172/182 class)

WHAT'S IN HERE
  runway_scout.py                  Pulls active vacant-land listings, scans terrain along
                                   wind-aligned headings with USGS elevation data, and scores
                                   each parcel for runway feasibility. Output: runway_candidates.csv
  Airstrip_Property_Analyzer.xlsx  Manual analyzer for finalists or listings found elsewhere.
                                   Fill the yellow cells; everything else calculates.

DATA SOURCES (all used within their terms, no scraping)
  RentCast API (listings; free developer tier)  https://app.rentcast.io
  USGS 3DEP Elevation Point Query Service (public domain, no key)

SETUP ON WINDOWS (about 15 minutes, no coding experience needed)
  1. Install Python from python.org. On the first installer screen, CHECK "Add python.exe to PATH".
     If you get the newer "Python install manager", go to Settings > Apps > Advanced app settings >
     App execution aliases and turn ON "Python (default)" and "Python install manager" (py.exe).
  2. Make a folder, e.g. C:\RunwayScout, and put these files in it.
  3. Create a RentCast account, ACTIVATE the free Developer plan, and create an API key.
     (A key without an activated plan gets rejected.)
  4. Open runway_scout.py in Notepad, find "api_key", and paste your key between the empty quotes.
     Change only what's between the quotes. Save.
  5. Open Command Prompt (Start > type cmd). Run:
         py -m pip install requests
         cd C:\RunwayScout
         py runway_scout.py
     Type commands into Command Prompt, NOT the Python installer window.
  6. Optional one-click runner: save these three lines in Notepad as run_scout.bat
     (Save as type: All Files):
         cd /d C:\RunwayScout
         py runway_scout.py
         pause

CUSTOMIZE
  Search areas, price cap, minimum acreage, aircraft, and safety margins are all in the CONFIG,
  AREAS, AIRCRAFT, and LEVERS sections near the top of the script. Edit values only, never the
  labels to the left of the colons. Default areas cover Central/Eastern Oregon and the WA side
  of the Columbia Gorge; swap in your own lat/lng centers.

THE MATH
  Density altitude = elevation + 120 x (OAT C - ISA temp at elevation)
  Required runway = max(POH takeoff over 50 ft, landing over 50 ft)
                    x 1.10 per 1,000 ft DA (compounded) x 1.15 grass x 1.25 pilot margin
  Terrain scan finds the longest run under 2% grade with under 3 ft deviation (compact-equipment
  cut/fill), on the prevailing-wind heading and +/- 30 degrees.

LIMITATIONS (read before you fall in love with a parcel)
  - Screening tool only. Verify with your POH, a site walk, and the county planning department.
  - Listing feeds have no parcel boundaries, so transects run through the listing point and are
    capped by a square-parcel estimate. Check finalists on the county GIS viewer.
  - Land-broker-only listings may not appear. Keep a LandWatch email alert as a backstop.
  - Zoning kills more airstrips than terrain. Call the county planner before the listing agent.
    Oregon: personal-use airport = conditional use on EFU land (ORS 215.283) + ODA registration.
    Washington: county CUP + WSDOT Aviation. Both: FAA Form 7480-1. Columbia River Gorge
    National Scenic Area parcels face an extra federal review layer.

DO NOT SHARE
  runway_candidates.csv and elevation_cache.json are generated on your machine and reveal
  exactly which parcels you've been looking at. Keep them, and your API key, to yourself.
