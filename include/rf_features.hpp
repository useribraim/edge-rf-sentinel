#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace edge_rf {

struct Reading {
    std::string timestamp;
    double time_seconds = 0.0;
    int sample = 0;
    int rank = 0;
    long long frequency_hz = 0;
    double frequency_mhz = 0.0;
    double power_db = 0.0;
    double baseline_db = 0.0;
    double delta_db = 0.0;
    double threshold_db = 0.0;
    double incident_min_power_db = -120.0;
    bool is_incident = false;
};

struct BurstFeature {
    std::string start_timestamp;
    std::string end_timestamp;
    double duration_seconds = 0.0;
    double center_frequency_mhz = 0.0;
    double min_frequency_mhz = 0.0;
    double max_frequency_mhz = 0.0;
    double cluster_width_khz = 0.0;
    double peak_power_db = -999.0;
    double peak_delta_db = -999.0;
    double mean_power_db = 0.0;
    double mean_delta_db = 0.0;
    double rise_rate_db_s = 0.0;
    double fall_rate_db_s = 0.0;
    double event_density_hz = 0.0;
    std::size_t snapshots = 0;
    std::size_t peak_bin_count = 0;
};

std::vector<Reading> read_readings_csv(const std::string& path);

std::vector<BurstFeature> extract_burst_features(
    const std::vector<Reading>& readings,
    double cluster_khz,
    double max_gap_seconds);

void write_features_csv(
    const std::string& path,
    const std::vector<BurstFeature>& features);

}  // namespace edge_rf
