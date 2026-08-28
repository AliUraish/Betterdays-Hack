-- Enrich staging -> fact table. Blank neighborhoods resolved with the polygon dictionary; category -> dimension.
INSERT INTO better_days.cases
SELECT
    case_id, opened, closed, status, agency, category, request_type, request_details, address,
    supervisor_district,
    if(neighborhood = '', dictGetOrDefault('better_days.nbhd_dict', 'name', (lon, lat), ''), neighborhood) AS neighborhood,
    analysis_nbhd, police_district, lat, lon, source,
    multiIf(
        category IN ('Street and Sidewalk Cleaning', 'Litter Receptacles', 'Illegal Postings'),                        'cleanliness',
        category IN ('Graffiti', 'Graffiti Public', 'Graffiti Private'),                                              'graffiti',
        category IN ('Encampments', 'Encampment', 'Homeless Concerns', 'Blocked Street or SideWalk'),                 'street_safety',
        category IN ('Noise Report'),                                                                                  'quiet',
        category IN ('Parking Enforcement', 'Abandoned Vehicle', 'Color Curb'),                                        'parking',
        category IN ('Streetlights', 'Street Defects', 'Sidewalk or Curb', 'Sewer Issues', 'Tree Maintenance',
                     'Damaged Property', 'Sign Repair', 'Catch Basin Maintenance'),                                    'infrastructure',
        'other') AS dimension
FROM better_days.cases_stage
WHERE lat BETWEEN 37.60 AND 37.85 AND lon BETWEEN -122.55 AND -122.35;   -- drop un-geocoded (0,0) and out-of-city points
