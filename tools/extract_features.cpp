#include "rf_features.hpp"

#include <exception>
#include <iostream>
#include <string>

namespace {

void usage(const char* program) {
    std::cerr
        << "Usage: " << program
        << " <readings.csv> <features.csv> [cluster_khz] [max_gap_seconds]\n\n"
        << "Example:\n"
        << "  " << program
        << " logs/rf_mobile_readings.csv features.csv 60 3\n";
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 3 || argc > 5) {
        usage(argv[0]);
        return 2;
    }

    const std::string input_path = argv[1];
    const std::string output_path = argv[2];
    const double cluster_khz = argc >= 4 ? std::stod(argv[3]) : 60.0;
    const double max_gap_seconds = argc >= 5 ? std::stod(argv[4]) : 3.0;

    try {
        const auto readings = edge_rf::read_readings_csv(input_path);
        const auto features = edge_rf::extract_burst_features(
            readings,
            cluster_khz,
            max_gap_seconds);
        edge_rf::write_features_csv(output_path, features);

        std::cout
            << "readings=" << readings.size()
            << " features=" << features.size()
            << " output=" << output_path << '\n';
    } catch (const std::exception& error) {
        std::cerr << "extract_features: " << error.what() << '\n';
        return 1;
    }

    return 0;
}
