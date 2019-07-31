// Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
// GNU Affero General Public License version 3 (see the file LICENSE).

package main

import (
    "fmt"
    "os"
    "encoding/json"

    "github.com/lxc/lxd/lxd/resources"
)

func main() {
    resources, err := resources.GetResources()
    if err != nil {
        fmt.Printf("error: %v\n", err)
        os.Exit(1)
    }

    data, err := json.MarshalIndent(resources, "", "\t")
    if err != nil {
        fmt.Printf("error: %v\n", err)
        os.Exit(1)
    }

    fmt.Printf("%s\n", data)
}