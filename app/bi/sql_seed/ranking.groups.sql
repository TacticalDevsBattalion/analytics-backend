-- title: Рейтинг екіпажів: польоти та втрати
-- description: Польоти, згруповані за всім, від чого залежить їх успіх (екіпаж, мета, день, результативність з джерела, основний результат, втрата, результати подій), і втрати за екіпажами та днями. Успіх груп визначають правила KPI у бекенді. Втрата чи подія бере підрозділ, мету й відсутні поля свого польоту за ідентифікатором (з будь-якого дня); польоти виключених цілей не рахуються. Фільтри застосовуються до кожного запису за його власними полями та часом.
WITH
    parents AS (
        SELECT unique_flight_id AS flight_id,
               toNullable(toString(any(bbak_id))) AS p_department, toNullable(toString(any(rota_id))) AS p_company,
               toNullable(any(crew)) AS p_team, toNullable(any(device_type)) AS p_device_type,
               toNullable(any(main_purpose)) AS p_main_purpose, toNullable(any(device)) AS p_device, toNullable(any(direction)) AS p_direction
        FROM pk_flight_list
        WHERE date BETWEEN {date_from:Date32} AND {date_to:Date32}
           OR unique_flight_id IN (
                SELECT unique_flight_id FROM pk_lost_devices WHERE date BETWEEN {date_from:Date32} AND {date_to:Date32}
                UNION ALL
                SELECT unique_flight_id FROM pk_events WHERE date BETWEEN {date_from:Date32} AND {date_to:Date32})
        GROUP BY flight_id
        -- A flight ID with two different organizations owns no children.
        HAVING uniqExact(tuple(bbak_id, rota_id, crew)) = 1
    ),
    records AS (
        SELECT 'flight' AS source, unique_flight_id AS flight_id, toString(date) AS day, device_type, toString(bbak_id) AS department,
               toString(rota_id) AS company, crew AS team, main_purpose, device, direction, is_effective, main_result,
               CAST(NULL AS Nullable(String)) AS result, CAST(NULL AS Nullable(String)) AS units,
               toUnixTimestamp(parseDateTimeBestEffortOrNull(toString(time_range), {tz:String})) AS local_timestamp
        FROM pk_flight_list
        WHERE date BETWEEN {date_from:Date32} AND {date_to:Date32} AND NOT has({excluded_purposes:Array(String)}, main_purpose)
        UNION ALL
        SELECT 'loss', l.unique_flight_id, toString(l.date), coalesce(l.device_type, p.p_device_type), p.p_department, p.p_company, p.p_team,
               p.p_main_purpose, coalesce(l.device, p.p_device), p.p_direction, NULL, NULL, NULL, NULL,
               if(extract(ifNull(l.time, ''), '(\\d{1,2}:\\d{2}(?::\\d{2}(?:\\.\\d+)?)?)') = '', NULL,
                  toUnixTimestamp(parseDateTimeBestEffortOrNull(concat(toString(l.date), ' ',
                      replaceRegexpOne(extract(ifNull(l.time, ''), '(\\d{1,2}:\\d{2}(?::\\d{2}(?:\\.\\d+)?)?)'), '^(\\d):', '0\\1:')), {tz:String})))
        FROM pk_lost_devices AS l LEFT JOIN parents AS p ON p.flight_id = l.unique_flight_id
        WHERE l.date BETWEEN {date_from:Date32} AND {date_to:Date32}
        UNION ALL
        -- An event's clock is its timestamp read as local time, then its own time of day.
        SELECT 'event', e.unique_flight_id, toString(e.date), p.p_device_type, p.p_department, p.p_company, p.p_team,
               coalesce(p.p_main_purpose, e.flight_purpose), coalesce(e.device, p.p_device), coalesce(e.direction, p.p_direction), NULL, NULL, e.result, e.units,
               coalesce(toUnixTimestamp(parseDateTimeBestEffortOrNull(toString(e.timestamp), {tz:String})),
                        if(extract(ifNull(e.time, ''), '(\\d{1,2}:\\d{2}(?::\\d{2}(?:\\.\\d+)?)?)') = '', NULL,
                           toUnixTimestamp(parseDateTimeBestEffortOrNull(concat(toString(e.date), ' ',
                               replaceRegexpOne(extract(ifNull(e.time, ''), '(\\d{1,2}:\\d{2}(?::\\d{2}(?:\\.\\d+)?)?)'), '^(\\d):', '0\\1:')), {tz:String}))))
        FROM pk_events AS e LEFT JOIN parents AS p ON p.flight_id = e.unique_flight_id
        WHERE e.date BETWEEN {date_from:Date32} AND {date_to:Date32}
    ),
    -- Filters: an empty list leaves the field unrestricted; a record without the field never matches a list.
    kept AS (
        SELECT * FROM records
        WHERE (empty({category_in:Array(String)}) OR has({category_in:Array(String)}, device_type))
          AND (empty({category_out:Array(String)}) OR (device_type IS NOT NULL AND NOT has({category_out:Array(String)}, device_type)))
          AND (empty({purpose_in:Array(String)}) OR has({purpose_in:Array(String)}, main_purpose))
          AND (empty({purpose_out:Array(String)}) OR (main_purpose IS NOT NULL AND NOT has({purpose_out:Array(String)}, main_purpose)))
          AND (empty({device_in:Array(String)}) OR has({device_in:Array(String)}, device))
          AND (empty({device_out:Array(String)}) OR (device IS NOT NULL AND NOT has({device_out:Array(String)}, device)))
          AND (empty({direction_in:Array(String)}) OR has({direction_in:Array(String)}, direction))
          AND (empty({direction_out:Array(String)}) OR (direction IS NOT NULL AND NOT has({direction_out:Array(String)}, direction)))
          AND ({time_from:Nullable(Float64)} IS NULL OR local_timestamp >= {time_from:Nullable(Float64)})
          AND ({time_to:Nullable(Float64)} IS NULL OR local_timestamp <= {time_to:Nullable(Float64)})
    ),
    flights AS (SELECT * FROM kept WHERE source = 'flight'),
    losses AS (SELECT * FROM kept WHERE source = 'loss'),
    events AS (SELECT flight_id, groupUniqArray(result) AS results, groupUniqArray(units) AS zones FROM kept WHERE source = 'event' GROUP BY flight_id)
-- detail = 1 when KPI rules match on the device or zones of a mission; otherwise they stay out of the groups.
SELECT 'flights' AS kind, f.device_type AS category, f.department AS department, f.company AS company, f.team AS team, f.main_purpose AS purpose, f.day AS day,
       f.is_effective AS is_effective, f.main_result AS main_result, f.flight_id IN (SELECT flight_id FROM losses) AS lost, arraySort(e.results) AS results,
       if({detail:UInt8} = 1, f.device, NULL) AS device, if({detail:UInt8} = 1, arraySort(e.zones), CAST([] AS Array(String))) AS units,
       uniqExact(f.flight_id) AS flights, toUInt64(0) AS lost_devices
FROM flights AS f LEFT JOIN events AS e ON e.flight_id = f.flight_id
GROUP BY category, department, company, team, purpose, day, is_effective, main_result, lost, results, device, units
UNION ALL
SELECT 'losses', device_type, department, NULL, team, main_purpose, day, NULL, NULL, 0, CAST([] AS Array(String)), NULL, CAST([] AS Array(String)), toUInt64(0), count()
FROM losses
GROUP BY device_type, department, team, main_purpose, day
