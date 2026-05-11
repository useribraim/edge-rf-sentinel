#include "rf_features.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <limits>
#include <map>
#include <sstream>
#include <stdexcept>
#include <unordered_map>

namespace edge_rf {
namespace {

std::vector<std::string> split_csv_line(const std::string& line) {
    std::vector<std::string> fields;
    std::string field;
    bool in_quotes = false;

    for (char ch : line) {
        if (ch == '"') {
            in_quotes = !in_quotes;
        } else if (ch == ',' && !in_quotes) {
            fields.push_back(field);
            field.clear();
        } else {
            field.push_back(ch);
        }
    }
    fields.push_back(field);
    return fields;
}

double parse_time_seconds(const std::string& timestamp) {
    if (timestamp.size() < 19) {
        return 0.0;
    }

    std::tm tm = {};
    std::istringstream input(timestamp.substr(0, 19));
    input >> std::get_time(&tm, "%Y-%m-%dT%H:%M:%S");
    if (input.fail()) {
        return 0.0;
    }

    return static_cast<double>(std::mktime(&tm));
}

double to_double(const std::string& value, double fallback = 0.0) {
    if (value.empty()) {
        return fallback;
    }
    try {
        return std::stod(value);
    } catch (...) {
        return fallback;
    }
}

int to_int(const std::string& value, int fallback = 0) {
    if (value.empty()) {
        return fallback;
    }
    try {
        return std::stoi(value);
    } catch (...) {
        return fallback;
    }
}

long long to_long_long(const std::string& value, long long fallback = 0) {
    if (value.empty()) {
        return fallback;
    }
    try {
        return std::stoll(value);
    } catch (...) {
        return fallback;
    }
}

bool truthy(const std::string& value) {
    if (value.empty()) {
        return false;
    }
    std::string lowered;
    lowered.reserve(value.size());
    for (char ch : value) {
        lowered.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(ch))));
    }
    return lowered == "1" || lowered == "true" || lowered == "yes";
}

std::string field(
    const std::vector<std::string>& row,
    const std::unordered_map<std::string, std::size_t>& header,
    const std::string& name) {
    const auto it = header.find(name);
    if (it == header.end() || it->second >= row.size()) {
        return "";
    }
    return row[it->second];
}

struct ClusterSnapshot {
    std::string timestamp;
    double time_seconds = 0.0;
    double center_frequency_mhz = 0.0;
    double min_frequency_mhz = 0.0;
    double max_frequency_mhz = 0.0;
    double peak_power_db = -999.0;
    double peak_delta_db = -999.0;
    double mean_power_db = 0.0;
    double mean_delta_db = 0.0;
    std::size_t bin_count = 0;
};

struct Track {
    std::vector<ClusterSnapshot> snapshots;
};

ClusterSnapshot make_snapshot(const std::vector<Reading>& cluster) {
    ClusterSnapshot snapshot;
    snapshot.timestamp = cluster.front().timestamp;
    snapshot.time_seconds = cluster.front().time_seconds;
    snapshot.min_frequency_mhz = cluster.front().frequency_mhz;
    snapshot.max_frequency_mhz = cluster.back().frequency_mhz;
    snapshot.bin_count = cluster.size();

    double weighted_sum = 0.0;
    double weight_total = 0.0;
    double power_sum = 0.0;
    double delta_sum = 0.0;

    for (const Reading& reading : cluster) {
        const double weight = std::max(0.001, reading.power_db + 120.0);
        weighted_sum += reading.frequency_mhz * weight;
        weight_total += weight;
        power_sum += reading.power_db;
        delta_sum += reading.delta_db;
        snapshot.peak_power_db = std::max(snapshot.peak_power_db, reading.power_db);
        snapshot.peak_delta_db = std::max(snapshot.peak_delta_db, reading.delta_db);
    }

    snapshot.center_frequency_mhz = weighted_sum / weight_total;
    snapshot.mean_power_db = power_sum / static_cast<double>(cluster.size());
    snapshot.mean_delta_db = delta_sum / static_cast<double>(cluster.size());
    return snapshot;
}

BurstFeature finalize_track(const Track& track) {
    BurstFeature feature;
    if (track.snapshots.empty()) {
        return feature;
    }

    const auto& snapshots = track.snapshots;
    const auto& first = snapshots.front();
    const auto& last = snapshots.back();
    const auto peak_it = std::max_element(
        snapshots.begin(),
        snapshots.end(),
        [](const ClusterSnapshot& left, const ClusterSnapshot& right) {
            return left.peak_delta_db < right.peak_delta_db;
        });

    feature.start_timestamp = first.timestamp;
    feature.end_timestamp = last.timestamp;
    feature.duration_seconds = std::max(0.0, last.time_seconds - first.time_seconds);
    feature.center_frequency_mhz = peak_it->center_frequency_mhz;
    feature.min_frequency_mhz = first.min_frequency_mhz;
    feature.max_frequency_mhz = first.max_frequency_mhz;
    feature.peak_power_db = -999.0;
    feature.peak_delta_db = -999.0;
    feature.snapshots = snapshots.size();
    feature.peak_bin_count = 0;

    double power_sum = 0.0;
    double delta_sum = 0.0;

    for (const ClusterSnapshot& snapshot : snapshots) {
        feature.min_frequency_mhz = std::min(feature.min_frequency_mhz, snapshot.min_frequency_mhz);
        feature.max_frequency_mhz = std::max(feature.max_frequency_mhz, snapshot.max_frequency_mhz);
        feature.peak_power_db = std::max(feature.peak_power_db, snapshot.peak_power_db);
        feature.peak_delta_db = std::max(feature.peak_delta_db, snapshot.peak_delta_db);
        feature.peak_bin_count = std::max(feature.peak_bin_count, snapshot.bin_count);
        power_sum += snapshot.mean_power_db;
        delta_sum += snapshot.mean_delta_db;
    }

    feature.cluster_width_khz = std::max(
        0.0,
        (feature.max_frequency_mhz - feature.min_frequency_mhz) * 1000.0);
    feature.mean_power_db = power_sum / static_cast<double>(snapshots.size());
    feature.mean_delta_db = delta_sum / static_cast<double>(snapshots.size());

    const double rise_seconds = peak_it->time_seconds - first.time_seconds;
    if (rise_seconds > 0.0) {
        feature.rise_rate_db_s = (peak_it->peak_delta_db - first.peak_delta_db) / rise_seconds;
    }

    const double fall_seconds = last.time_seconds - peak_it->time_seconds;
    if (fall_seconds > 0.0) {
        feature.fall_rate_db_s = (last.peak_delta_db - peak_it->peak_delta_db) / fall_seconds;
    }

    return feature;
}

std::string format_double(double value) {
    std::ostringstream output;
    output << std::fixed << std::setprecision(3) << value;
    return output.str();
}

}  // namespace

std::vector<Reading> read_readings_csv(const std::string& path) {
    std::ifstream input(path);
    if (!input) {
        throw std::runtime_error("failed to open readings CSV: " + path);
    }

    std::string line;
    if (!std::getline(input, line)) {
        return {};
    }

    const auto columns = split_csv_line(line);
    std::unordered_map<std::string, std::size_t> header;
    for (std::size_t index = 0; index < columns.size(); ++index) {
        header[columns[index]] = index;
    }

    std::vector<Reading> readings;
    while (std::getline(input, line)) {
        if (line.empty()) {
            continue;
        }

        const auto row = split_csv_line(line);
        Reading reading;
        reading.timestamp = field(row, header, "timestamp");
        reading.time_seconds = parse_time_seconds(reading.timestamp);
        reading.sample = to_int(field(row, header, "sample"));
        reading.rank = to_int(field(row, header, "rank"));
        reading.frequency_hz = to_long_long(field(row, header, "frequency_hz"));
        reading.frequency_mhz = to_double(field(row, header, "frequency_mhz"));
        reading.power_db = to_double(field(row, header, "power_db"));
        reading.baseline_db = to_double(field(row, header, "baseline_db"));
        reading.delta_db = to_double(field(row, header, "delta_db"));
        reading.threshold_db = to_double(field(row, header, "threshold_db"));
        reading.incident_min_power_db = to_double(
            field(row, header, "incident_min_power_db"),
            -120.0);
        reading.is_incident = truthy(field(row, header, "is_incident"));

        if (!reading.is_incident) {
            reading.is_incident =
                reading.delta_db >= reading.threshold_db &&
                reading.power_db >= reading.incident_min_power_db;
        }

        readings.push_back(reading);
    }

    return readings;
}

std::vector<BurstFeature> extract_burst_features(
    const std::vector<Reading>& readings,
    double cluster_khz,
    double max_gap_seconds) {
    std::map<std::string, std::vector<Reading>> by_timestamp;
    for (const Reading& reading : readings) {
        if (reading.is_incident) {
            by_timestamp[reading.timestamp].push_back(reading);
        }
    }

    std::vector<Track> active_tracks;
    std::vector<BurstFeature> features;
    const double cluster_mhz = cluster_khz / 1000.0;

    for (auto& [timestamp, timestamp_readings] : by_timestamp) {
        std::sort(
            timestamp_readings.begin(),
            timestamp_readings.end(),
            [](const Reading& left, const Reading& right) {
                return left.frequency_mhz < right.frequency_mhz;
            });

        std::vector<ClusterSnapshot> snapshots;
        std::vector<Reading> current_cluster;
        for (const Reading& reading : timestamp_readings) {
            if (current_cluster.empty()) {
                current_cluster.push_back(reading);
                continue;
            }

            const double gap_mhz = reading.frequency_mhz - current_cluster.back().frequency_mhz;
            if (gap_mhz <= cluster_mhz) {
                current_cluster.push_back(reading);
            } else {
                snapshots.push_back(make_snapshot(current_cluster));
                current_cluster = {reading};
            }
        }
        if (!current_cluster.empty()) {
            snapshots.push_back(make_snapshot(current_cluster));
        }

        std::vector<bool> matched(active_tracks.size(), false);
        for (const ClusterSnapshot& snapshot : snapshots) {
            int best_index = -1;
            double best_distance = std::numeric_limits<double>::max();

            for (std::size_t index = 0; index < active_tracks.size(); ++index) {
                if (matched[index] || active_tracks[index].snapshots.empty()) {
                    continue;
                }
                const ClusterSnapshot& tail = active_tracks[index].snapshots.back();
                const double time_gap = snapshot.time_seconds - tail.time_seconds;
                const double distance =
                    std::abs(snapshot.center_frequency_mhz - tail.center_frequency_mhz);
                if (time_gap <= max_gap_seconds && distance <= cluster_mhz && distance < best_distance) {
                    best_distance = distance;
                    best_index = static_cast<int>(index);
                }
            }

            if (best_index >= 0) {
                active_tracks[static_cast<std::size_t>(best_index)].snapshots.push_back(snapshot);
                matched[static_cast<std::size_t>(best_index)] = true;
            } else {
                Track track;
                track.snapshots.push_back(snapshot);
                active_tracks.push_back(track);
                matched.push_back(true);
            }
        }

        std::vector<Track> still_active;
        for (const Track& track : active_tracks) {
            const ClusterSnapshot& tail = track.snapshots.back();
            if (!snapshots.empty() && snapshots.front().time_seconds - tail.time_seconds > max_gap_seconds) {
                features.push_back(finalize_track(track));
            } else {
                still_active.push_back(track);
            }
        }
        active_tracks = std::move(still_active);
    }

    for (const Track& track : active_tracks) {
        features.push_back(finalize_track(track));
    }

    std::sort(
        features.begin(),
        features.end(),
        [](const BurstFeature& left, const BurstFeature& right) {
            return left.start_timestamp < right.start_timestamp;
        });
    return features;
}

void write_features_csv(
    const std::string& path,
    const std::vector<BurstFeature>& features) {
    std::ofstream output(path);
    if (!output) {
        throw std::runtime_error("failed to open feature output CSV: " + path);
    }

    output
        << "start_timestamp,end_timestamp,duration_seconds,center_frequency_mhz,"
        << "min_frequency_mhz,max_frequency_mhz,cluster_width_khz,peak_power_db,"
        << "peak_delta_db,mean_power_db,mean_delta_db,rise_rate_db_s,"
        << "fall_rate_db_s,snapshots,peak_bin_count\n";

    for (const BurstFeature& feature : features) {
        output
            << feature.start_timestamp << ','
            << feature.end_timestamp << ','
            << format_double(feature.duration_seconds) << ','
            << format_double(feature.center_frequency_mhz) << ','
            << format_double(feature.min_frequency_mhz) << ','
            << format_double(feature.max_frequency_mhz) << ','
            << format_double(feature.cluster_width_khz) << ','
            << format_double(feature.peak_power_db) << ','
            << format_double(feature.peak_delta_db) << ','
            << format_double(feature.mean_power_db) << ','
            << format_double(feature.mean_delta_db) << ','
            << format_double(feature.rise_rate_db_s) << ','
            << format_double(feature.fall_rate_db_s) << ','
            << feature.snapshots << ','
            << feature.peak_bin_count << '\n';
    }
}

}  // namespace edge_rf
